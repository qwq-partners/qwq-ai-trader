"""Regression coverage for sector-briefing price evidence.

All provider cache paths are redirected before a provider is constructed so
these tests cannot read or write an operator's cached market data.
"""

import asyncio
import json
from datetime import date, timedelta

import pytest

from src.data.providers import sector_momentum
from src.data.providers.sector_momentum import SectorMomentumProvider
from src.experts.sector_council import SectorCouncilExpert
from src.experts.types import ExpertConfig


def _isolate_provider_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(sector_momentum, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(sector_momentum, "_SECTOR_MAP_CACHE", tmp_path / "sector_map.json")
    monkeypatch.setattr(sector_momentum, "_ETF_MOMENTUM_CACHE", tmp_path / "etf_momentum.json")
    monkeypatch.setattr(
        sector_momentum, "_ETF_MOMENTUM_MULTI_CACHE", tmp_path / "etf_momentum_multi.json"
    )


def _bars(count, latest=89.54, prior=86.643, r5_base=99.8884):
    """Oldest-first daily bars with a positive final day but negative 5/20d returns."""
    start = date(2026, 9, 1)
    prices = [100.0 - i * 0.1 for i in range(count)]
    prices[0] = 100.0
    if count >= 6:
        prices[-6] = r5_base
    if count >= 2:
        prices[-2] = prior
    prices[-1] = latest
    return [
        {"stck_bsop_date": (start + timedelta(days=i)).isoformat(), "stck_clpr": value}
        for i, value in enumerate(prices)
    ]


class _BarsBroker:
    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    async def get_daily_prices(self, symbol, days=25):
        self.calls.append((symbol, days))
        return self.bars


def test_quant_only_blend_keeps_negative_multiday_evidence_when_last_day_is_positive(
    monkeypatch, tmp_path
):
    """A change that treats the last +3.34% day as the sector trend must fail this test."""
    _isolate_provider_paths(monkeypatch, tmp_path)
    expert = SectorCouncilExpert(ExpertConfig())
    expert._provider = SectorMomentumProvider(broker=_BarsBroker(_bars(21)))

    async def _no_qualitative():
        return {}

    monkeypatch.setattr(expert, "_fetch_qual_signals", _no_qualitative)
    opinion = asyncio.run(expert._analyze())
    evidence = opinion.raw_evidence["sector_scores"]["반도체"]

    assert evidence["r1"] == pytest.approx(3.34, abs=0.01)
    assert evidence["r5"] == pytest.approx(-10.36, abs=0.01)
    assert evidence["r20"] == pytest.approx(-10.46, abs=0.01)
    assert evidence["score"] == -80  # clip((.4*r5 + .6*r20)*10) * .8
    assert evidence["quant_only"] is True
    assert evidence["score_saturated"] is True
    assert evidence["as_of"] == "2026-09-21"
    assert evidence["source"] == "KIS:091160"
    assert evidence["r5_sessions"] == 5
    assert evidence["r20_sessions"] == 20
    assert opinion.data_status == "partial"
    assert "섹터 뉴스 재료" in opinion.missing_inputs


def test_multi_cache_persists_price_date_and_provenance(monkeypatch, tmp_path):
    """A cache reload must retain the observed bar date instead of inferring it from file mtime."""
    _isolate_provider_paths(monkeypatch, tmp_path)
    first = SectorMomentumProvider(broker=_BarsBroker(_bars(21)))
    written = asyncio.run(first.get_all_sector_momentum_multi())["반도체"]

    second = SectorMomentumProvider(broker=None)
    restored = asyncio.run(second.get_all_sector_momentum_multi())["반도체"]

    assert restored["as_of"] == "2026-09-21"
    assert restored["source"] == "KIS:091160"
    assert restored["r1"] == pytest.approx(written["r1"])
    assert restored["r20_start"] == "2026-09-01"


def test_partial_history_labels_actual_return_intervals(monkeypatch, tmp_path):
    """A short history must not present its three-session return as a full 5/20-session window."""
    _isolate_provider_paths(monkeypatch, tmp_path)
    provider = SectorMomentumProvider(broker=_BarsBroker(_bars(4, latest=110, prior=105)))

    result = asyncio.run(provider._calc_etf_returns("091160"))

    assert result["r1"] == pytest.approx((110 - 105) / 105 * 100)
    assert result["r20"] == pytest.approx(10.0)
    assert result["r5"] is None
    assert result["r5_sessions"] == 3
    assert result["r20_sessions"] == 3
    assert result["r5_start"] == "2026-09-01"
    assert result["r20_start"] == "2026-09-01"


def test_legacy_cache_has_explicitly_unknown_price_provenance(monkeypatch, tmp_path):
    """A change that fabricates as_of from a legacy cache's mtime must fail this test."""
    _isolate_provider_paths(monkeypatch, tmp_path)
    (tmp_path / "etf_momentum.json").write_text(
        json.dumps({"반도체": -10.46}), encoding="utf-8"
    )
    provider = SectorMomentumProvider(broker=None)

    restored = asyncio.run(provider.get_all_sector_momentum_multi())["반도체"]

    assert restored["r20"] == -10.46
    assert restored["as_of"] is None
    assert restored["source"] == "unknown"


def test_all_refresh_failures_do_not_republish_stale_sector_facts(monkeypatch, tmp_path):
    """A failed full refresh must return unavailable evidence, never the prior values as fresh."""
    _isolate_provider_paths(monkeypatch, tmp_path)
    provider = SectorMomentumProvider(broker=_BarsBroker([]))
    provider._etf_momentum = {"반도체": 9.0}
    provider._etf_momentum_multi = {
        "반도체": {"r20": 9.0, "as_of": "2026-09-21", "source": "KIS:091160"}
    }

    unavailable = asyncio.run(provider.get_all_sector_momentum_multi())

    assert unavailable == {}
    assert provider._etf_momentum == {}


def test_nonfinite_price_does_not_become_positive_sector_evidence(monkeypatch, tmp_path):
    """A NaN close must be unavailable rather than leaking through comparisons as positive evidence."""
    _isolate_provider_paths(monkeypatch, tmp_path)
    provider = SectorMomentumProvider(
        broker=_BarsBroker([
            {"stck_bsop_date": "2026-09-01", "stck_clpr": 100},
            {"stck_bsop_date": "2026-09-02", "stck_clpr": float("nan")},
        ])
    )

    assert asyncio.run(provider._calc_etf_returns("091160")) is None
