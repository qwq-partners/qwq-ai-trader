"""Theme supply metadata keeps unobserved symbols unknown instead of numeric zero.

Producer: ``ThemeChasingStrategy._refresh_supply_demand`` builds per-side caches from
the KIS foreign/institution *ranking* lists (FHPTJ04400000). A symbol absent from a
ranking list, or a row whose quantity failed validation (provider forwards
``net_buy_qty=0`` with ``_supply_proof=None``), is unobserved — not a measured zero.
Consumers (``cross_validator`` missing-indicator check, ``entry_llm_context``
direction) treat any non-None value as observed, so a forwarded 0 became a false
"balanced"/available reading. Observed 0 and negative quantities must survive.
"""
import json
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import src.core.cross_validator as cross_validator_module
import src.strategies.kr.theme_chasing as theme_module
from src.core.entry_llm_context import build_entry_context
from src.strategies.kr.theme_chasing import ThemeChasingConfig, ThemeChasingStrategy


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 5, 10, 30)
        return value if tz is None else value.replace(tzinfo=tz)


def _row(symbol, qty):
    # Mirrors kis_market_data.fetch_foreign_institution output for a validated quantity.
    return {"symbol": symbol, "name": "합성", "net_buy_qty": qty,
            "_supply_proof": {"value": qty, "observed_monotonic": time.monotonic()}}


def _invalid_row(symbol):
    # Provider: ``net_qty = proof["value"] if proof is not None else 0`` → legacy 0, proof None.
    return {"symbol": symbol, "name": "합성", "net_buy_qty": 0, "_supply_proof": None}


class _FakeMarketData:
    """One foreign market empty (failure/empty list), the other markets partially populated."""

    def __init__(self):
        self.calls = []
        self.rows = {
            ("0001", "1"): [],
            ("0002", "1"): [_row("000001", 100), _invalid_row("000003")],
            ("0001", "2"): [_row("000002", -5), _row("000001", 0)],
            ("0002", "2"): [],
        }

    async def fetch_foreign_institution(self, market, investor):
        self.calls.append((market, investor))
        return list(self.rows[(market, investor)])


class _FakeDetector:
    def get_stock_themes(self, symbol):
        return ["합성테마"]

    def get_stock_sentiment(self, symbol):
        return None


@pytest.fixture
def strategy(monkeypatch):
    monkeypatch.setattr(theme_module, "datetime", _FixedDateTime)
    strat = ThemeChasingStrategy(ThemeChasingConfig(max_entries_per_theme=10), kis_market_data=_FakeMarketData())
    strat.set_theme_detector(_FakeDetector())
    strat._active_themes["합성테마"] = {"name": "합성테마", "related_stocks": [], "score": 85.0,
                                    "last_updated": _FixedDateTime.now()}
    strat._theme_entries["합성테마"] = 0
    strat._entries_date = date.today()  # generate_signal resets themes on a new day
    return strat


async def _signal(strategy, symbol):
    strategy._indicators[symbol] = {"change_1d": 3.0, "vol_ratio": 2.5, "high": 10050.0, "volume": 100_000,
                                    "atr_14": 2.0, "rsi_14": 50.0, "ma20": 9800.0}
    signal = await strategy.generate_signal(symbol, Decimal("10000"))
    assert signal is not None, symbol
    return signal


def _supply(signal):
    ind = signal.metadata["indicators"]
    return ind["foreign_net_buy"], ind["inst_net_buy"]


def _facts(prompt):
    return json.loads(prompt.split("<entry_facts>\n", 1)[1].split("\n</entry_facts>", 1)[0])


@pytest.mark.asyncio
async def test_absent_symbol_forwards_none_while_observed_zero_and_negative_survive(strategy):
    absent_foreign = await _signal(strategy, "000002")   # inst observed -5, not in foreign list
    observed = await _signal(strategy, "000001")         # foreign 100, inst observed 0
    unobserved = await _signal(strategy, "000003")       # invalid foreign row, absent from inst

    assert len(strategy._foreign_cache) == 2 and len(strategy._institution_cache) == 2  # caches nonempty
    assert _supply(absent_foreign) == (None, -5)
    assert _supply(observed) == (100, 0)
    assert _supply(unobserved) == (None, None)
    # Bonus/penalty arithmetic is unchanged: unobserved sides contribute nothing, observed sides score.
    assert observed.score_breakdown["supply_bonus"] == 5.0
    assert absent_foreign.score_breakdown["supply_bonus"] == 0.0
    assert unobserved.score_breakdown["supply_bonus"] == 0.0


@pytest.mark.asyncio
async def test_consumers_read_forwarded_none_as_missing_not_balanced(strategy, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    import datetime as datetime_module
    monkeypatch.setattr(cross_validator_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(datetime_module, "datetime", _FixedDateTime)

    absent_foreign = await _signal(strategy, "000002")
    observed = await _signal(strategy, "000001")
    unobserved = await _signal(strategy, "000003")

    directions = {}
    for signal in (absent_foreign, observed, unobserved):
        facts = _facts(build_entry_context(signal.symbol, "theme_chasing", signal.score,
                                           signal.metadata["indicators"], "bull", "합성"))
        directions[signal.symbol] = (facts["indicators"]["foreign_net_buy"]["status"],
                                     facts["indicators"]["foreign_net_buy"]["direction"])
    assert directions == {"000002": ("missing", "unknown"), "000001": ("provided", "net_buy"),
                          "000003": ("missing", "unknown")}

    validator = cross_validator_module.CrossStrategyValidator(market="KR")
    validator._load_panel_outlook = lambda: None
    base = {"atr_pct": 3.0, "per": 10.0, "pbr": 1.0}
    results = {
        signal.symbol: validator.validate(symbol=signal.symbol, side="buy", strategy="theme_chasing", score=70,
                                          metadata={**base, "indicators": signal.metadata["indicators"]},
                                          market_regime="bull")
        for signal in (absent_foreign, observed, unobserved)
    }
    # One observed side (even negative) is available evidence; both-unobserved gets the existing missing-supply step.
    assert results["000002"][1] == results["000001"][1]
    assert results["000003"][1] == results["000001"][1] - validator._MISSING_IND_PENALTY_STEP
    assert all(ok for ok, _score, _reason in results.values())
