"""Disabled strategy rows must not consume another strategy's candidate slot."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.batch_analyzer import BatchAnalyzer
from src.indicators.technical import TechnicalIndicators
from src.signals.screener.swing_screener import SwingScreener
from src.signals.strategic.vcp_detector import VCPDetector


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def row(*, sepa=True):
    # A short pullback can be oversold while still above rising long averages.
    ind = dict(close=11000, ma50=10000, ma150=9000, ma200=8000,
               high_52w=12000, low_52w=7000, change_60d=10, rsi_2=5,
               high_20d=11500, low_20d=10500)
    if not sepa:
        ind["ma50"] = 12000  # RSI2 remains valid; SEPA price condition fails.
    ind["sepa_pass"], ind["sepa_reasons"] = TechnicalIndicators.check_sepa(ind)
    assert ind["sepa_pass"] is sepa
    assert TechnicalIndicators.check_rsi2_entry(ind)[0]
    return dict(symbol="000010", name="synthetic", indicators=ind)


def wire_scan(monkeypatch, screener, rows):
    # Keep the real filters, VCP exclusion, symbol dedupe, and orchestration.
    # Stub source IO / unrelated scoring; no candidate filter is replaced.
    monkeypatch.setattr(screener, "_load_benchmark_index", AsyncMock())
    monkeypatch.setattr(screener, "_build_universe", AsyncMock(return_value=rows))
    monkeypatch.setattr(screener, "_calculate_all_indicators", AsyncMock(return_value=rows))
    vcp = SimpleNamespace(symbol="000010", name="synthetic", score=80,
                          ma_aligned=True, vol_declining=True, contraction_count=3,
                          high_proximity=0.9, reasons=[])
    monkeypatch.setattr(VCPDetector, "detect_all", lambda self, data: [vcp])

    async def unchanged(candidates):
        return candidates

    monkeypatch.setattr(screener, "_apply_composite_score", unchanged)
    monkeypatch.setattr(screener, "_compute_lci_zscore", lambda candidates: None)
    monkeypatch.setattr(screener, "_apply_strategic_overlay", unchanged)
    return screener


@pytest.mark.parametrize("rsi2,sepa,vcp,expected", [
    (False, True, True, "sepa_trend"),
    (True, True, True, "rsi2_reversal"),  # Preserve active tie priority.
    (True, False, True, "rsi2_reversal"),
    (False, False, True, "vcp_breakout"),
    (False, False, False, None),
])
def test_enabled_rows_own_dedupe_and_vcp_exclusion(monkeypatch, rsi2, sepa, vcp, expected):
    sc = SwingScreener(None, None, config={
        "rsi2_reversal": {"enabled": rsi2}, "sepa_trend": {"enabled": sepa},
        "vcp_breakout": {"enabled": vcp},
    })
    wire_scan(monkeypatch, sc, [row()])
    candidates = asyncio.run(sc.run_full_scan())
    assert [c.strategy for c in candidates] == ([expected] if expected else [])


def test_disabled_rsi2_does_not_exclude_vcp_when_sepa_filter_fails(monkeypatch):
    sc = SwingScreener(None, None, config={
        "rsi2_reversal": {"enabled": False}, "sepa_trend": {"enabled": True},
        "vcp_breakout": {"enabled": True},
    })
    wire_scan(monkeypatch, sc, [row(sepa=False)])
    candidates = asyncio.run(sc.run_full_scan())
    assert [c.strategy for c in candidates] == ["vcp_breakout"]
    assert float(candidates[0].entry_price) == pytest.approx(11557.5)


@pytest.mark.parametrize("config", [None, {}, {"rsi2_reversal": {}, "sepa_trend": {}}])
def test_missing_flags_follow_batch_defaults(monkeypatch, config):
    # Batch already defaults RSI2 off; SEPA retains its existing default on.
    sc = wire_scan(monkeypatch, SwingScreener(None, None, config=config), [row()])
    assert [c.strategy for c in asyncio.run(sc.run_full_scan())] == ["sepa_trend"]


def test_batch_config_preserves_active_candidate_until_signal_generation(monkeypatch, tmp_path):
    # Construct the real consumer: it must pass flags into the real screener.
    monkeypatch.setattr("src.data.providers.sector_momentum.SectorMomentumProvider",
                        lambda **kwargs: None)
    ba = BatchAnalyzer(None, None, None, config={
        "rsi2_reversal": {"enabled": False}, "sepa_trend": {"enabled": True},
        "vcp_breakout": {"enabled": False},
        "llm_ops": {"batch_llm_filter_enabled": False},
    })
    wire_scan(monkeypatch, ba._screener, [row()])
    monkeypatch.setattr(ba, "_save_pit_snapshot", lambda candidates: None)
    monkeypatch.setattr("src.data.providers.disclosure_feed.refresh_disclosure_cache",
                        AsyncMock(return_value=0))
    rsi2_signals = AsyncMock(return_value=[])
    sepa_signals = AsyncMock(return_value=[])
    monkeypatch.setattr(ba._rsi2, "generate_batch_signals", rsi2_signals)
    monkeypatch.setattr(ba._sepa, "generate_batch_signals", sepa_signals)
    asyncio.run(ba._scan_and_build(expire_today=True))
    rsi2_signals.assert_awaited_once_with([])
    received = sepa_signals.call_args.args[0]
    assert [(c.symbol, c.strategy) for c in received] == [("000010", "sepa_trend")]
