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
import dataclasses
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
from src.core.evolution.backtest_gate import (  # noqa: E402
    MAX_MDD_WORSENING, MIN_RETURN_GAIN, MIN_TRADES, BacktestGate, WF_SEGMENTS)
from src.core.evolution.backtest_gate import WF_MIN_WINS as GATE_WF_MIN_WINS  # noqa: E402
from src.utils.config import effective_config_hash, load_effective_config  # noqa: E402

AXES = [("ladder", "channel"), ("current", "extended"), ("nominal", "risk")]
BASELINE = "ladder/current/nominal"
MDD_TOLERANCE_PP = 3.0
WF_MIN_WINS = 2

# 사전 등록 대조군(T7-B): 고정 명목 비중 = 위험률 0.7% ÷ 동시 5슬롯 = 14%.
# risk 모드의 "노출 축소" 효과를 사이징 공식과 분리해 보기 위한 축이며,
# 결과를 보고 값을 바꾸지 않는다.
FIXED_NOMINAL_PCT = 14.0
SIZING_MODES = ("nominal", "risk", "nominal14")


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
    if sizing == "nominal14":
        # 대조군: 명목 사이징이되 비중을 0.7/5 = 14% 로 고정 (base=max 로 상한도 같이 내린다)
        cfg.sizing = "nominal"
        cfg.base_position_pct = FIXED_NOMINAL_PCT
        cfg.max_position_pct = FIXED_NOMINAL_PCT
    else:
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
                   universe=None, cache_files=(), missing_tickers=(),
                   cache_substitutes=None, regime_sources=None) -> dict:
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
        # missing: OHLCV 를 못 얻어 시뮬레이션에서 빠진 종목 (offline 부분 캐시·상장폐지 등).
        # 생존편향·데이터 누락을 유추가 아니라 명시로 남긴다 (T7-A, 2026-09-14 리뷰 advisory).
        "universe": {"size": args.universe_size,
                     "tickers": list(universe if universe is not None else []),
                     "missing_tickers": list(missing_tickers)},
        "ohlcv_cache": {Path(f).name: _file_hash(Path(f)) for f in cache_files},
        # substitutes: 정확한 파일명(종료일 포함) 캐시가 없어 **구간을 포함하는** 다른 캐시를
        # 잘라 쓴 종목. 어떤 파일을 대신 썼는지 남겨야 같은 결과를 다시 만들 수 있다.
        "ohlcv_cache_substitutes": dict(cache_substitutes or {}),
        # 레짐 시계열 출처 — 지수(kospi_pykrx/kospi_fdr) vs 개별주 대리(samsung_proxy) 구분.
        # 캐시가 없어 이번 실행 범위에서 새로 만들었다면 생성 시각·출처가 함께 남는다.
        "regime_sources": dict(regime_sources or {}),
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
        engine.regime.source = shared["regime_sources"][f"{cfg.months}m"]
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
    shared["missing_tickers"] = sorted({*shared.get("missing_tickers", []),
                                        *engine.universe.missing_tickers})
    shared.setdefault("cache_substitutes", {}).update(engine.universe.cache_substitutes)
    shared.setdefault("regime_sources", {})[f"{cfg.months}m"] = engine.regime.source
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
        # 운영 게이트의 최소 거래 수는 이벤트(매도) 단위다 — 포지션 단위와 구분해 함께 남긴다
        "total_trades": m["total_trades"],
        "avg_equity": float(sum(e for _, e in engine.equity_curve) / len(engine.equity_curve)),
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


def benchmark_return(start: str, end: str, *, offline: bool = False) -> dict:
    """KODEX200(069500) 매수보유 수익률 — 리뷰 판정 기준(초과수익). 실패 시 KS11 폴백.

    offline 에서는 KODEX200 이 개별 종목이라 다운로드하지 않는다. 캐시가 있으면 쓰고,
    없으면 초과수익은 **미측정(null)** 이다 — 0 으로 채워 통과시키지 않는다 (계획서 T8).
    """
    if offline:
        um = bt.UniverseManager(size=1, offline=True)
        hit = um._find_covering_cache("069500", start, end)
        if hit is not None:
            df, path = hit
            c = df["종가"].astype(float)
            return {"code": "069500", "return_pct": (c.iloc[-1] / c.iloc[0] - 1) * 100,
                    "start": str(df.index[0])[:10], "end": str(df.index[-1])[:10],
                    "source": f"cache:{path.name}"}
        return {"code": None, "return_pct": None,
                "source": "offline: KODEX200 캐시 없음 — 초과수익 미측정"}
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


def index_proxy_return(kospi_df, start: str, end: str, source: str) -> dict:
    """레짐 캐시의 KOSPI 지수 매수보유 수익률 — KODEX200 이 없을 때의 **참고** 지표.

    ETF(보수·추적오차·배당)와 지수는 같지 않으므로 KODEX200 초과수익을 이것으로 대체하지
    않는다. 같은 윈도우의 모든 셀에 같은 값이 빠지므로 셀 간 비교에는 영향이 없다.
    """
    if kospi_df is None or "종가" not in getattr(kospi_df, "columns", []):
        return {"code": None, "return_pct": None, "source": source}
    import pandas as pd
    win = kospi_df.loc[(kospi_df.index >= pd.Timestamp(start)) & (kospi_df.index <= pd.Timestamp(end))]
    if len(win) < 2:
        return {"code": None, "return_pct": None, "source": source}
    c = win["종가"].astype(float)
    return {"code": "KOSPI", "return_pct": (c.iloc[-1] / c.iloc[0] - 1) * 100,
            "start": str(win.index[0])[:10], "end": str(win.index[-1])[:10],
            "source": source}


def ops_gate(cell: dict, base: dict) -> dict:
    """운영 게이트 판정 (`backtest_gate` 와 같은 임계, 연구 판정과 **다른 기준**).

    총수익 개선 > 0pp AND MDD 악화 ≤ 1pp AND WF 승수 ≥ 2/3 AND 거래(매도 이벤트) ≥ 10.
    임계는 게이트 모듈에서 그대로 가져온다 — 여기서 완화하지 않는다.
    """
    gain = cell["total_return_pct"] - base["total_return_pct"]
    mdd_delta = abs(cell["mdd_pct"]) - abs(base["mdd_pct"])
    wf = (sum(1 for c, b in zip(cell["segments"] or [], base["segments"] or []) if c > b)
          if cell["segments"] and base["segments"] else None)
    checks = {
        "trades": cell["total_trades"] >= MIN_TRADES,
        "return_gain": gain > MIN_RETURN_GAIN,
        "wf": (wf is not None and wf >= GATE_WF_MIN_WINS),
        "mdd": mdd_delta <= MAX_MDD_WORSENING,
    }
    return {"gain_pp": gain, "mdd_delta_pp": mdd_delta, "wf_wins": wf,
            "checks": checks, "passed": all(checks.values()),
            "thresholds": {"min_return_gain_pp": MIN_RETURN_GAIN,
                           "max_mdd_worsening_pp": MAX_MDD_WORSENING,
                           "min_trades": MIN_TRADES, "wf_min_wins": GATE_WF_MIN_WINS}}


def judge(cell: dict, base: dict) -> dict:
    """baseline 대비 **연구** 판정: 기대값·PF 우위 AND WF≥2/3 AND MDD 악화 ≤3pp."""
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
           "| 보유중앙 | 회전(연) | 수수료드래그 | WF승 | 연구판정 | 운영게이트 |")
    sep = "|" + "---|" * 20
    rows = [hdr, sep]
    for name, c in cells.items():
        p, j = c["position"], c["judge"]
        wf = "—" if name == BASELINE else (f"{j['wf_wins']}/3" if j["wf_wins"] is not None else "n/a")
        verdict = "기준" if name == BASELINE else ("**통과**" if j["beats_baseline"] else
                  "미달(" + ",".join(k for k, v in j["checks"].items() if not v) + ")")
        g = c.get("ops_gate")
        ops = "기준" if name == BASELINE or not g else (
            "**통과**" if g["passed"] else "미달(" + ",".join(k for k, v in g["checks"].items() if not v) + ")")
        ex = c.get("excess_return_pct")
        rows.append(
            f"| {name} | {p['trades']} | {p['win_rate']:.1f}% | {p['avg_win_pct']:+.2f}% | {p['avg_loss_pct']:+.2f}% "
            f"| {p['payoff']:.2f} | {p['expectancy_pct']:+.2f}% | {p['expectancy_r']:+.2f} | {p['profit_factor']:.2f} "
            f"| {c['total_return_pct']:+.2f}% | {'n/a' if ex is None else f'{ex:+.2f}pp'} | {c['sharpe']:.2f} "
            f"| {c['mdd_pct']:.2f}% | {p['max_consec_losses']} | {p['median_holding_days']:.0f}일 "
            f"| {p['turnover_annual']:.1f}x | {p['fee_drag_pct']:.2f}% | {wf} | {verdict} | {ops} |")
    return "\n".join(rows)


def md_strategy_table(cells: dict) -> str:
    rows = ["| 셀 | 전략 | 거래 | 승률 | 기대값% | 기대값R | PF | 순손익 | 보유중앙 |", "|---|---|---|---|---|---|---|---|---|"]
    for name, c in cells.items():
        for s, p in c["per_strategy"].items():
            rows.append(f"| {name} | {s} | {p['trades']} | {p['win_rate']:.1f}% | {p['expectancy_pct']:+.2f}% "
                        f"| {p['expectancy_r']:+.2f} | {p['profit_factor']:.2f} | {p['net_pnl']:+,.0f} | {p['median_holding_days']:.0f}일 |")
    return "\n".join(rows)


def recompute_from_saved(cell_dir: Path) -> dict:
    """저장된 `fills/equity/config` **만으로** 포지션 지표를 다시 계산한다 (T7-B 필수 조건).

    요약 JSON 을 신뢰하지 않고 원자료에서 R·PF·비용·회전·상위3 제외를 되만들 수 있는지
    확인하는 경로다. 독립 리뷰어도 같은 함수로 표를 재계산할 수 있다.
    """
    names = {f.name for f in dataclasses.fields(bt.BacktestConfig)}
    raw = json.loads((cell_dir / "config.json").read_text(encoding="utf-8"))
    cfg = bt.BacktestConfig(**{k: v for k, v in raw.items() if k in names})
    fills = [bt.Trade(**f) for f in json.loads((cell_dir / "fills.json").read_text(encoding="utf-8"))]
    curve = [(d, float(e)) for d, e in json.loads((cell_dir / "equity.json").read_text(encoding="utf-8"))]
    an = bt.ResultAnalyzer(cfg, fills, curve)
    ps = an.positions()
    m = an.metrics()
    top3 = sorted(ps, key=lambda p: -p["pnl"])[:3]
    return {
        "positions": ps,
        "position_metrics": an.position_metrics(ps),
        "total_return_pct": m["total_return_pct"], "mdd_pct": m["mdd_pct"],
        "total_trades": m["total_trades"], "total_fees": m["total_fees"],
        "net_pnl_excl_top3": sum(p["pnl"] for p in ps) - sum(p["pnl"] for p in top3),
        "segments": BacktestGate._segment_returns(curve, WF_SEGMENTS),
    }


def verify_saved_outputs(outdir: Path, tol: float = 1e-6) -> dict:
    """`--verify-dir`: 저장된 셀 원자료 재계산 결과와 summary.json 을 대조한다."""
    summary = json.loads((outdir / "summary.json").read_text(encoding="utf-8"))
    saved = {}
    for slabel, grid in summary["grids"].items():
        for wname, w in grid["windows"].items():
            for cell, c in w["cells"].items():
                saved[(f"{slabel}_{wname}", cell)] = c
    checks, mismatches = {}, []
    for (wname, cell), c in saved.items():
        d = outdir / wname / cell.replace("/", "_")
        if not d.exists():
            mismatches.append(f"{wname} {cell}: 원자료 디렉터리 없음 ({d})")
            continue
        r = recompute_from_saved(d)
        diffs = {k: (c[k], v) for k, v in (
            ("total_return_pct", r["total_return_pct"]), ("mdd_pct", r["mdd_pct"]),
            ("total_trades", r["total_trades"]), ("total_fees", r["total_fees"]),
        ) if abs(float(c[k]) - float(v)) > tol}
        for k in ("trades", "expectancy_r", "profit_factor", "net_pnl", "turnover_annual"):
            if abs(float(c["position"][k]) - float(r["position_metrics"][k])) > tol:
                diffs[f"position.{k}"] = (c["position"][k], r["position_metrics"][k])
        checks[f"{wname}/{cell}"] = {"ok": not diffs, "diffs": diffs,
                                     "net_pnl_excl_top3": r["net_pnl_excl_top3"]}
        if diffs:
            mismatches.append(f"{wname} {cell}: {diffs}")
    return {"checked": len(checks), "all_match": not mismatches,
            "mismatches": mismatches, "cells": checks}


# 저장소에 커밋된 연구 원본 — 재실행이 조용히 덮어쓰면 안 된다 (2026-09-14 리뷰 blocking #3)
PROTECTED_RESULTS = (bt.RESULTS_DIR / "ab_exit_policy_2026-09.json",)


def resolve_out_path(args: argparse.Namespace) -> Path:
    """요약 JSON 저장 경로. 기본은 <output-dir>/summary.json, 원본 경로는 --overwrite 필수."""
    out = Path(args.out) if args.out is not None else Path(args.output_dir) / "summary.json"
    if not args.overwrite and out.resolve() in {p.resolve() for p in PROTECTED_RESULTS}:
        raise SystemExit(
            f"거부: {out} 는 커밋된 연구 원본이다. 다른 --out 을 쓰거나 --overwrite 를 명시할 것.")
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="청산×보유×사이징 2×2×2 A/B")
    ap.add_argument("--months", default="6,12")
    ap.add_argument("--strategies", nargs="+", default=["sepa,rsi2", "sepa"],
                    help="전략 집합(콤마 구분)별로 그리드를 따로 돈다")
    ap.add_argument("--universe-size", type=int, default=60)
    ap.add_argument("--holding", default="current,extended",
                    help="보유 정책 축 (기본 2×2×2; 'none' 추가 시 보유 규칙 해제 보충 셀)")
    ap.add_argument("--exit-policy", default=",".join(AXES[0]),
                    help="청산 정책 축 (기본 ladder,channel)")
    ap.add_argument("--sizing", default=",".join(AXES[2]),
                    help=f"사이징 축 (기본 nominal,risk; 선택 nominal14 = 고정 {FIXED_NOMINAL_PCT}% 명목 대조군)")
    ap.add_argument("--verify-dir", default=None,
                    help="실행하지 않고, 저장된 셀 원자료로 summary.json 을 재계산해 대조한다")
    ap.add_argument("--out", default=None,
                    help="요약 JSON 경로 (기본: <output-dir>/summary.json). "
                         "원본 결과 파일을 가리키면 --overwrite 없이는 거부한다")
    ap.add_argument("--overwrite", action="store_true",
                    help="보호된 원본 결과 파일 덮어쓰기 허용 (기본 금지)")
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
    if args.verify_dir:
        report = verify_saved_outputs(Path(args.verify_dir))
        path = Path(args.verify_dir) / "recompute_check.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
        print(f"재계산 대조 {report['checked']}셀 → {'일치' if report['all_match'] else '불일치'}: {path}")
        for m in report["mismatches"]:
            print("  " + m)
        raise SystemExit(0 if report["all_match"] else 1)

    effective = load_effective_config(args.effective_config)
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    out = resolve_out_path(args)          # 원본 덮어쓰기는 실행 전에 거부한다

    sizings = tuple(args.sizing.split(","))
    unknown = [s for s in sizings if s not in SIZING_MODES]
    if unknown:
        raise SystemExit(f"거부: 알 수 없는 사이징 축 {unknown} (가능: {list(SIZING_MODES)})")
    axes = [tuple(args.exit_policy.split(",")), tuple(args.holding.split(",")), sizings]
    windows = [int(m) for m in args.months.split(",")]
    result = {"run_at": datetime.now().isoformat(timespec="seconds"),
              "universe_size": args.universe_size, "baseline": BASELINE,
              "axes": {"exit_policy": axes[0], "holding_policy": axes[1], "sizing": axes[2]},
              "gate": {"wf_min_wins": WF_MIN_WINS, "mdd_tolerance_pp": MDD_TOLERANCE_PP,
                       "rule": "기대값%·PF > baseline AND WF≥2/3 AND MDD 악화 ≤3pp, 모든 윈도우"},
              "ops_gate": {"rule": "총수익 개선 >0pp AND MDD 악화 ≤1pp AND WF≥2/3 AND 거래(매도 이벤트) ≥10",
                           "thresholds": {"min_return_gain_pp": MIN_RETURN_GAIN,
                                          "max_mdd_worsening_pp": MAX_MDD_WORSENING,
                                          "min_trades": MIN_TRADES,
                                          "wf_min_wins": GATE_WF_MIN_WINS}},
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
                shared[bench_key] = benchmark_return(base["start_date"], base["end_date"],
                                                     offline=args.offline)
            bench = shared[bench_key]
            proxy = index_proxy_return(shared.get(("regime", months)), base["start_date"],
                                       base["end_date"],
                                       shared.get("regime_sources", {}).get(f"{months}m", "unknown"))
            for name, c in cells.items():
                c["judge"] = judge(c, base)
                c["ops_gate"] = ops_gate(c, base)
                c["benchmark"] = bench
                c["excess_return_pct"] = (c["total_return_pct"] - bench["return_pct"]
                                          if bench["return_pct"] is not None else None)
                c["benchmark_proxy"] = proxy
                c["excess_return_proxy_pct"] = (c["total_return_pct"] - proxy["return_pct"]
                                                if proxy["return_pct"] is not None else None)
            grid["windows"][f"{months}m"] = {
                "period": [base["start_date"], base["end_date"]], "n_days": base["n_days"],
                "benchmark": bench, "benchmark_proxy": proxy, "cells": cells,
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
        ops_passing = [n for n in names if n != BASELINE
                       and all(w["cells"][n]["ops_gate"]["passed"] for w in grid["windows"].values())]
        grid["verdict"] = {"passing_cells": passing,
                           "ops_gate_passing_cells": ops_passing,
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
        print(f"\n[{strategies}] 연구 통과 셀: {passing or '없음'} → winner: {grid['verdict']['winner']}")
        print(f"[{strategies}] 운영 게이트 통과 셀: {ops_passing or '없음'}\n")

    payload = json.dumps(result, ensure_ascii=False, indent=1, default=str)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(payload, encoding="utf-8")

    # 실행 입력·요약 보존 (T7-A). 커밋된 원본 결과는 --overwrite 없이 건드리지 않는다.
    manifest = build_manifest(args, effective, universe=shared.get("tickers"),
                              cache_files=shared.get("cache_files", []),
                              missing_tickers=shared.get("missing_tickers", []),
                              cache_substitutes=shared.get("cache_substitutes", {}),
                              regime_sources=shared.get("regime_sources", {}))
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if out.resolve() != (outdir / "summary.json").resolve():
        (outdir / "summary.json").write_text(payload, encoding="utf-8")
    print(f"저장: {out} · {outdir}  (총 {time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
