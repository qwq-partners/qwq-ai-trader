"""유효 설정 기반 백테스트 설정·운영 게이트 (2026-09-14 리뷰 후속 T6 — F7).

기준 결함(F7): BacktestGate 가 기본 BacktestConfig(nominal·TP1 5%/0.30·손절 3.5~6·
allocation sepa .6/rsi2 .1/core .3) 로 baseline/candidate 를 만들어, 실운영(risk 0.7%/18%·
TP1 10%/0.10·손절 4~8·sepa 40/gap 15/vcp 10) 과 다른 기준군 위에서 판정했다.
→ risk.risk_per_trade_pct 0.7→0.8, risk_max_position_pct 18→10 이 포지션 금액을 전혀 바꾸지 못하고
  (양쪽 다 nominal) "수익률 개선 없음"으로 자동 기각됐다.

여기서는 default.yml + evolved_overrides.yml 을 실제 우선순위로 병합한 **유효 설정**으로
BacktestConfig 를 만들고, 게이트가 그 설정을 기준군으로 쓰는지, 지원하지 못하는 범위를
조용히 통과시키지 않는지 고정한다. 네트워크·KIS·운영 상태 파일 무접촉(백테스트 실행은 대역으로 대체).

실행: venv/bin/python -m pytest tests/test_backtest_gate_config.py -q -p no:cacheprovider
"""

import asyncio
import copy
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.evolution import backtest_gate as gate_mod  # noqa: E402
from src.utils.config import effective_config_hash, load_effective_config  # noqa: E402
from src.utils.fee_calculator import get_fee_calculator  # noqa: E402


def _load_bt():
    spec = importlib.util.spec_from_file_location(
        "_bt_strategies_for_gate_test", ROOT / "scripts" / "backtest_strategies.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


bt = _load_bt()
EFFECTIVE = load_effective_config()


def _build(effective=None, *, months=6, strategies=("sepa",), **kw):
    return bt.build_backtest_config_from_effective(
        effective if effective is not None else EFFECTIVE,
        months=months, strategies=list(strategies), **kw)


# ── 유효 설정 → BacktestConfig 매핑 ────────────────────────────────────────────

def test_builder_mirrors_live_sizing_exit_and_fees():
    cfg = _build()
    # 사이징 (risk 모드가 nominal 로 되돌아가지 않는다)
    assert (cfg.sizing, cfg.risk_per_trade_pct, cfg.risk_max_position_pct) == ("risk", 0.7, 18.0)
    assert (cfg.base_position_pct, cfg.max_position_pct) == (25.0, 28.0)
    # 초기 손절 — 전략별 고정 SL (실엔진 신규 fill 미러)
    assert cfg.entry_stop_mode == "live_policy" and cfg.sepa_stop_loss_pct == 5.0
    # 청산 사다리·복합 청산·익절후 저효율·stale (실엔진 값)
    assert (cfg.first_exit_pct, cfg.first_exit_ratio) == (10.0, 0.1)
    assert (cfg.min_stop_pct, cfg.max_stop_pct) == (4.0, 8.0)
    assert cfg.enable_composite_exit is True
    assert (cfg.post_exit_stale_days, cfg.post_exit_stale_pnl_pct) == (5, 3.0)
    assert (cfg.stale_exit_days, cfg.stale_exit_pnl_pct) == (5, 2.0)
    assert (cfg.stale_high_days, cfg.stale_high_min_pnl_pct) == (3, 3.0)
    # 현금 제약·최소 금액·슬롯
    assert (cfg.min_cash_reserve_pct, cfg.min_position_value) == (5.0, 200_000)
    assert cfg.slot_policy == "live_weighted" and cfg.max_positions_weighted == 8
    # 수수료는 FeeCalculator 단일 출처와 같은 값이어야 한다
    fee = get_fee_calculator("KR")
    assert float(fee.config.buy_commission_rate) == bt.BUY_FEE_RATE
    assert float(fee.config.total_sell_rate) == bt.SELL_FEE_RATE


def test_builder_drops_zero_allocation_and_reports_unsupported_strategies():
    cfg = _build(strategies=("sepa", "rsi2", "core"))
    assert cfg.strategies == ["sepa"]                     # rsi2·core 배분 0%
    assert cfg.allocation == {"sepa": 1.0}
    scope = cfg.supported_scope
    assert scope["strategies_simulated"] == ["sepa"]
    assert scope["excluded_zero_allocation"] == ["rsi2", "core"]
    # 배분이 있지만 백테스터가 모사하지 못하는 라인은 결과에 남는다 (부분 검증임을 감춘다 X)
    assert scope["unsupported_allocated"] == {"gap_and_go": 15.0, "vcp_breakout": 10.0}
    assert 0 < scope["allocation_covered_pct"] < 100


def test_builder_requires_at_least_one_supported_active_strategy():
    eff = copy.deepcopy(EFFECTIVE)
    eff["kr"]["risk"]["strategy_allocation"] = {"gap_and_go": 15.0, "sepa_trend": 0.0}
    with pytest.raises(bt.UnsupportedBacktestConfig) as e:
        _build(eff, strategies=("sepa", "rsi2"))
    assert "배분" in str(e.value)


def test_builder_rejects_fee_model_divergence(monkeypatch):
    monkeypatch.setattr(bt, "BUY_FEE_RATE", 0.0005)
    with pytest.raises(bt.UnsupportedBacktestConfig) as e:
        _build()
    assert "수수료" in str(e.value)


def test_builder_rejects_unknown_sizing_mode():
    eff = copy.deepcopy(EFFECTIVE)
    eff["kr"]["risk"]["sizing_mode"] = "kelly"
    with pytest.raises(bt.UnsupportedBacktestConfig):
        _build(eff)


# ── risk 파라미터가 실제로 포지션 금액을 바꾼다 (F7 재현) ────────────────────────

def _one_fill(cfg_kw, *, stop_pct=5.0):
    """합성 1종목·1주문으로 체결 수량을 얻는다 (네트워크 없음)."""
    import pandas as pd
    idx = pd.bdate_range("2026-01-05", periods=40)
    close = 10000.0
    df = pd.DataFrame({"시가": close, "고가": close * 1.011, "저가": close * 0.989,
                       "종가": close, "거래량": 1_000_000.0}, index=idx)
    cfg = _build(**cfg_kw)
    cfg.sepa_stop_loss_pct = stop_pct
    eng = bt.BacktestEngine(cfg)
    eng.universe.tickers = ["X"]
    eng.universe.names = {"X": "X"}
    eng.universe.ohlcv = {"X": bt.BTIndicators.compute(df)}
    eng.regime.kospi_data = None
    eng.cash = 10_000_000.0
    eng.pending_buys = [{"symbol": "X", "strategy": bt.StrategyType.SEPA, "score": 80.0,
                         "signal_close": close, "signal_date": "2026-02-13",
                         "indicator_asof": "2026-02-13"}]
    eng._execute_pending_buys("2026-02-16")
    return eng.positions["X"].quantity


def test_risk_per_trade_pct_change_moves_position_size():
    q07 = _one_fill({})
    q08 = _one_fill({"risk_per_trade_pct": 0.8})
    assert q07 == 139 and q08 == 159        # equity 1천만 × r% / SL 5% (매수수수료 포함 상한)


def test_risk_max_position_pct_change_moves_position_size():
    """상한이 실제로 걸리는 손절폭(3%) — 0.7/3 = 23.3% > 18% 이므로 cap 이 수량을 정한다."""
    q18 = _one_fill({}, stop_pct=3.0)
    q10 = _one_fill({"risk_max_position_pct": 10.0}, stop_pct=3.0)
    assert q18 == 180 and q10 == 100


# ── 게이트: 기준군·지원 범위·coverage ──────────────────────────────────────────

def _curve(n=60, ret_per_seg=1.0, start=10_000_000.0):
    """n 포인트 자산 곡선 (구간별 동일 상승)."""
    out, eq = [], start
    for i in range(n):
        eq *= (1 + ret_per_seg / 100 / 20)
        out.append((f"d{i}", eq))
    return out


def _metrics(ret=5.0, mdd=-5.0, trades=20, curve=None):
    """구간 수익률은 ret 에 비례하게 만들어 WF 구간승이 총수익 방향과 일치하게 한다."""
    return {"total_return_pct": ret, "mdd_pct": mdd, "total_trades": trades,
            "win_rate": 50.0, "profit_factor": 1.5, "sharpe": 1.0,
            "_equity_curve": curve if curve is not None else _curve(ret_per_seg=ret)}


def _gate_with(monkeypatch, results):
    """_run_once 를 대역으로 교체하고 (config, metrics) 기록. results=[baseline, candidate]"""
    g = gate_mod.BacktestGate(enabled=True)
    seen = []

    def fake_run(module, cfg):
        seen.append(copy.deepcopy(cfg))
        return results[len(seen) - 1]

    monkeypatch.setattr(g, "_run_once", fake_run)
    monkeypatch.setattr(g, "_load_module", lambda: bt)
    return g, seen


def _verify(g, **change):
    base = {"strategy": "sepa", "parameter": "min_score", "old_value": 60, "new_value": 65}
    base.update(change)
    return asyncio.run(g.verify(base))


def test_gate_baseline_is_effective_config_and_candidate_differs_only_in_field(monkeypatch):
    g, seen = _gate_with(monkeypatch, [_metrics(ret=5.0), _metrics(ret=8.0)])
    r = _verify(g, strategy="risk", parameter="risk_per_trade_pct",
                old_value=0.7, new_value=0.8)
    base, cand = seen
    assert base.sizing == "risk" and base.risk_per_trade_pct == 0.7      # nominal 로 되돌아가지 않음
    assert cand.risk_per_trade_pct == 0.8
    diffs = {k: (getattr(base, k), getattr(cand, k)) for k in vars(base)
             if getattr(base, k) != getattr(cand, k)}
    assert set(diffs) == {"risk_per_trade_pct"}
    assert r.config_hash == effective_config_hash(load_effective_config())
    assert r.diff == {"risk_per_trade_pct": [0.7, 0.8]}
    assert r.supported_scope["strategies_simulated"] == ["sepa"]
    assert r.passed is True and r.skipped is False


def test_gate_holds_when_strategy_is_not_backtestable(monkeypatch):
    """gap/vcp 는 백테스터가 모사하지 못한다 — skipped=True 성공으로 처리하지 않는다."""
    g, _ = _gate_with(monkeypatch, [])
    r = _verify(g, strategy="gap_and_go", parameter="stop_loss_pct",
                old_value=3.5, new_value=3.0)
    assert r.passed is False and r.skipped is False
    assert "미지원" in r.reason and "gap_and_go" in r.reason


def test_gate_holds_on_unknown_config_field(monkeypatch):
    g, _ = _gate_with(monkeypatch, [])
    monkeypatch.setitem(gate_mod.PARAM_MAP, "sepa.min_score", "not_a_field")
    r = _verify(g)
    assert r.passed is False and r.skipped is False and "not_a_field" in r.reason


def test_gate_holds_when_walk_forward_cannot_be_evaluated(monkeypatch):
    """WF 생략 경로가 승인 근거가 되지 않는다 — 곡선이 짧으면 보류하고 coverage 를 남긴다."""
    short = _curve(n=10)
    g, _ = _gate_with(monkeypatch, [_metrics(ret=5.0, curve=short),
                                    _metrics(ret=9.0, curve=short)])
    r = _verify(g)
    assert r.passed is False and "walk-forward" in r.reason
    assert r.coverage["wf_windows_evaluated"] == 0
    assert r.coverage["equity_points_baseline"] == 10


def test_gate_judgment_rules_unchanged(monkeypatch):
    """운영 판정(총수익 개선·MDD 악화 ≤1pp·거래 ≥10)은 그대로 — 연구 기준(기대값·PF)으로 대체 금지."""
    g, _ = _gate_with(monkeypatch, [_metrics(ret=5.0), _metrics(ret=5.0)])
    assert _verify(g).passed is False                      # 동률 → 기각
    g, _ = _gate_with(monkeypatch, [_metrics(ret=5.0), _metrics(ret=9.0, trades=5)])
    assert _verify(g).passed is False                      # 표본 부족
    g, _ = _gate_with(monkeypatch, [_metrics(ret=5.0, mdd=-5.0),
                                    _metrics(ret=9.0, mdd=-6.5)])
    r = _verify(g)
    assert r.passed is False and "MDD" in r.reason
    g, _ = _gate_with(monkeypatch, [_metrics(ret=5.0, mdd=-5.0),
                                    _metrics(ret=9.0, mdd=-5.5)])
    assert _verify(g).passed is True


def test_gate_holds_when_effective_config_is_unsupported(monkeypatch):
    g, _ = _gate_with(monkeypatch, [])
    eff = copy.deepcopy(EFFECTIVE)
    eff["kr"]["risk"]["strategy_allocation"] = {"gap_and_go": 15.0}
    monkeypatch.setattr(gate_mod, "load_effective_config", lambda: eff)
    r = _verify(g, strategy="risk", parameter="risk_per_trade_pct",
                old_value=0.7, new_value=0.8)
    assert r.passed is False and r.errored is True and "배분" in r.reason


def test_gate_replay_override_dict_uses_effective_baseline(monkeypatch):
    """사후 재생(gate_replay)이 넘기는 overrides dict 도 유효 설정 기준군 위에서 돈다."""
    g = gate_mod.BacktestGate(enabled=True)
    seen = {}
    def fake_engine(cfg):
        seen["cfg"] = cfg
        return _FakeEngine()

    monkeypatch.setattr(bt, "BacktestEngine", fake_engine)
    g._module = bt
    g._run_once(bt, {"sepa_min_score": 70.0, "months": 3})
    cfg = seen["cfg"]
    assert cfg.sizing == "risk" and cfg.entry_stop_mode == "live_policy"
    assert (cfg.sepa_min_score, cfg.months) == (70.0, 3)


class _FakeEngine:
    equity_curve = []

    def run(self, save_results=True):
        return {}


# ── T7-A 러너 옵션과 manifest ─────────────────────────────────────────────────

def _load_ab():
    spec = importlib.util.spec_from_file_location(
        "_ab_exit_policy_for_test", ROOT / "scripts" / "ab_exit_policy.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ab = _load_ab()


def test_runner_defaults_are_offline_ready_and_point_in_time():
    ns = ab.build_parser().parse_args([])
    assert ns.end_date == "2026-09-11"                     # 마지막 완결 거래일 (기존 연구와 동일)
    assert ns.entry_stop_mode == "live_policy"
    assert ns.slot_policy == "live_weighted"
    assert ns.offline is False and ns.effective_config is None
    assert ns.output_dir.endswith("ab_exit_policy_review_v2")


def test_runner_manifest_matches_cli_args(tmp_path):
    ns = ab.build_parser().parse_args(
        ["--offline", "--months", "6", "--universe-size", "20",
         "--entry-stop-mode", "live_policy", "--slot-policy", "fixed",
         "--end-date", "2026-09-11", "--output-dir", str(tmp_path)])
    man = ab.build_manifest(ns, EFFECTIVE, cache_files=[])
    assert man["cli_args"] == vars(ns)
    assert man["end_date"] == "2026-09-11" and man["offline"] is True
    assert man["entry_stop_mode"] == "live_policy" and man["slot_policy"] == "fixed"
    assert man["config_hash"] == effective_config_hash(EFFECTIVE)
    assert man["config_snapshot"]["kr"]["risk"]["sizing_mode"] == "risk"
    assert man["calculator_version"] == bt.CALC_VERSION
    assert man["random_used"] is False
    assert man["ohlcv_cache"] == {}
    assert len(man["integration_sha"]) >= 7


def test_runner_offline_never_downloads(tmp_path, monkeypatch):
    """캐시 없는 오프라인 실행은 다운로드로 자동 전환하지 않고 데이터 부족으로 종료한다."""
    monkeypatch.setattr(bt, "CACHE_DIR", tmp_path / "empty")

    def boom(*a, **k):
        raise AssertionError("offline 인데 네트워크 호출")

    monkeypatch.setattr(bt.pykrx_stock, "get_market_ohlcv_by_date", boom)
    monkeypatch.setattr(bt.pykrx_stock, "get_market_cap_by_ticker", boom)
    monkeypatch.setattr(bt.pykrx_stock, "get_market_ticker_name", boom)
    um = bt.UniverseManager(size=5, use_cache=True, offline=True)
    um.build_universe("20260101")                 # 하드코딩 유니버스 — 네트워크 없음
    assert len(um.tickers) == 5
    with pytest.raises(bt.BacktestDataUnavailable):
        um.load_ohlcv("20250101", "20260911")


def test_runner_offline_regime_requires_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "CACHE_DIR", tmp_path / "empty")

    def boom(*a, **k):
        raise AssertionError("offline 인데 네트워크 호출")

    monkeypatch.setattr(bt.pykrx_stock, "get_index_ohlcv_by_date", boom)
    with pytest.raises(bt.BacktestDataUnavailable):
        bt.MarketRegime(offline=True).load("20250101", "20260911")
