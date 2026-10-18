"""Unknown swing supply remains unknown at each downstream consumer boundary."""
import json
from datetime import datetime
from pathlib import Path

import src.core.cross_validator as cross_validator_module
from src.core.entry_llm_context import build_entry_context
from src.core.evolution.trade_journal import TradeJournal
from src.core.evolution.trade_memory import TradeMemory


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 18, 14, 0)
        return value if tz is None else value.replace(tzinfo=tz)


def _private_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))


def _facts(prompt):
    encoded = prompt.split("<entry_facts>\n", 1)[1].split("\n</entry_facts>", 1)[0]
    return json.loads(encoded)


def test_cross_validator_penalizes_unknown_supply_but_observes_explicit_zero(tmp_path, monkeypatch):
    """Changing the None guard to truthiness would falsely mark observed zero as missing."""
    _private_home(monkeypatch, tmp_path)
    import datetime as datetime_module

    monkeypatch.setattr(cross_validator_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(datetime_module, "datetime", _FixedDateTime)
    validator = cross_validator_module.CrossStrategyValidator(market="KR")
    validator._load_panel_outlook = lambda: None
    base = {"atr_pct": 3.0, "per": 10.0, "pbr": 1.0}

    unknown = validator.validate(
        symbol="005930", side="buy", strategy="core_holding", score=70,
        metadata={**base, "indicators": {"foreign_net_buy": None, "inst_net_buy": None}},
        market_regime="bull",
    )
    observed_zero = validator.validate(
        symbol="005930", side="buy", strategy="core_holding", score=70,
        metadata={**base, "indicators": {"foreign_net_buy": 0, "inst_net_buy": 0}},
        market_regime="bull",
    )

    assert unknown == (True, 68, "")
    assert observed_zero == (True, 70, "")
    assert validator.get_stats()["penalized"] == 1


def test_entry_llm_context_marks_unknown_swing_supply_missing(tmp_path, monkeypatch):
    """Replacing None with zero would falsely describe unobserved supply as balanced."""
    _private_home(monkeypatch, tmp_path)

    packet = _facts(build_entry_context(
        "005930", "strategic_swing", 80,
        {"foreign_net_buy": None, "inst_net_buy": None}, "neutral", "반도체",
    ))

    supply = packet["indicators"]["foreign_net_buy"]
    assert supply == {
        "value": None,
        "unit": "source_unit_unverified",
        "status": "missing",
        "source_observed_at": None,
        "direction": "unknown",
    }


def test_trade_memory_persists_unknown_swing_supply_without_positive_inference(tmp_path, monkeypatch):
    """Treating None as a numeric supply value would corrupt the stored learning pattern."""
    _private_home(monkeypatch, tmp_path)
    memory = TradeMemory()
    indicators = {"foreign_net_buy": None, "inst_net_buy": None, "rsi_14": 55}

    memory.record_outcome(
        symbol="005930", name="합성", strategy="strategic_swing", sector="반도체",
        entry_date="2026-10-01", exit_date="2026-10-02", holding_days=1,
        pnl_pct=1.0, exit_type="take_profit", entry_indicators=indicators,
    )
    layer1_path = tmp_path / ".cache" / "ai_trader" / "trade_memory" / "layer1.json"
    assert json.loads(layer1_path.read_text())[0]["entry_indicators"] == indicators

    memory._layer1[0].timestamp = "2026-10-01T00:00:00"
    assert memory._compress_to_layer2() == 1
    assert "외국인매수" not in memory._layer2[0].pattern


def test_trade_journal_writer_and_reload_preserve_unknown_swing_supply(tmp_path, monkeypatch):
    """Replacing null indicators during serialization would make a later review use invented data."""
    _private_home(monkeypatch, tmp_path)
    indicators = {"foreign_net_buy": None, "inst_net_buy": None}
    journal = TradeJournal(storage_dir=tmp_path / "journal")

    journal.record_entry(
        trade_id="synthetic-supply", symbol="005930", name="합성",
        entry_price=70000, entry_quantity=1, entry_reason="synthetic",
        entry_strategy="strategic_swing", indicators=indicators,
    )

    (path,) = (tmp_path / "journal").glob("trades_*.json")
    assert json.loads(path.read_text())["trades"][0]["indicators_at_entry"] == indicators
    reloaded = TradeJournal(storage_dir=tmp_path / "journal")
    assert reloaded.get_trade("synthetic-supply").indicators_at_entry == indicators
