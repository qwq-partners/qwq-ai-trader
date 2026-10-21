"""배치 선정 근거가 대기 JSON과 실행 Signal 경계를 통과하는 회귀 검증."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from src.core.batch_analyzer import BatchAnalyzer, PendingSignal
from src.core.types import OrderSide, Signal, SignalStrength, StrategyType
from test_t11_entry_plan import NOW, SYM, _analyzer, _freeze_clock

CAPTURED = "2026-09-15T01:20:00+00:00"
CALCULATED = "2026-09-14T15:40:00"


def _indicators():
    # Swing valid_evidence의 실제 형식. 수신 시각과 공급자 기준일을 구별한다.
    return {
        "mrs": 3.25, "mrs_slope": 0.5, "lci": None,
        "foreign_net_buy": 0, "inst_net_buy": None, "supply_data_age": None,
        "supply_evidence": {
            "foreign": {"value": 0, "source": "FHPTJ04400000",
                        "source_date": None, "captured_at": CAPTURED,
                        "status": "provisional", "market": "0001"},
            "institution": None,
        },
        "supply_coverage": {"ranking_rows": 1, "broker_estimate_rows": 0},
    }


def _pending(ba, indicators):
    signal = Signal(
        symbol=SYM, side=OrderSide.BUY, strength=SignalStrength.STRONG,
        strategy=StrategyType.SEPA_TREND, price=Decimal("10000"),
        stop_price=Decimal("9500"), target_price=Decimal("11000"),
        score=70, reason="합성 근거", metadata={"indicators": indicators},
    )
    return ba._to_pending_signal(signal, now=NOW,
                                 expires=NOW + timedelta(hours=5), slippage_pct=3)


@pytest.fixture
def analyzer(monkeypatch, tmp_path):
    import src.core.batch_analyzer as bam
    _freeze_clock(monkeypatch, bam)
    ba = _analyzer(monkeypatch, tmp_path)
    # 기존 fixture의 메모리 대체를 제거하여 실제 원자적 저장/재로드를 검증한다.
    del ba._save_json
    del ba._load_json
    ba._signals_path = tmp_path / "pending.json"
    ba._screener = SimpleNamespace(_indicators=SimpleNamespace(
        _cache={SYM: {"mrs": -99, "foreign_net_buy": 999}},
        _cache_ts={SYM: bam.datetime.fromisoformat(CALCULATED)},
    ))
    return ba


def _execute(ba, monkeypatch):
    events = []

    async def emit(event):
        events.append(event)

    async def quote(_symbol):
        return {"price": 10100}

    async def positions():
        return {}

    async def nxt():
        return set()

    async def no_sleep(_seconds):
        pass

    ba._engine = SimpleNamespace(
        portfolio=SimpleNamespace(positions={}), emit=emit, _stock_name_cache={})
    ba._broker = SimpleNamespace(get_quote=quote, get_positions=positions,
                                 get_nxt_symbols=nxt)
    ba._sector_momentum = None
    ba._config = {"batch": {"signal_interval_sec": 0}}
    monkeypatch.setattr("src.core.batch_analyzer.asyncio.sleep", no_sleep)
    asyncio.run(ba.execute_pending_signals())
    assert len(events) == 1
    return events[0].signal.metadata


def test_restart_executes_original_enriched_indicators(analyzer, monkeypatch, tmp_path):
    original = _indicators()
    analyzer._pending = [_pending(analyzer, original)]
    # 계획 생성 후 후보/공용캐시 변조가 저장·실행 근거에 영향을 주면 안 된다.
    original["mrs"] = -1
    original["supply_evidence"]["foreign"]["captured_at"] = "2099-01-01T00:00:00+00:00"
    analyzer._save_json()
    assert json.loads(analyzer._signals_path.read_text())[0]["inputs_ref"]

    restarted = _analyzer(monkeypatch, tmp_path)
    del restarted._save_json
    del restarted._load_json
    restarted._signals_path = analyzer._signals_path
    restarted._screener = SimpleNamespace(_indicators=SimpleNamespace(
        _cache={SYM: {"mrs": -777, "foreign_net_buy": 123}}, _cache_ts={SYM: NOW}))
    meta = _execute(restarted, monkeypatch)
    assert meta["indicators"] == _indicators()
    assert meta["indicators_source"] == "pending_snapshot"
    assert meta["indicators_as_of"] == CALCULATED
    assert meta["quote_as_of"] == NOW.isoformat()
    assert meta["indicators"]["foreign_net_buy"] == 0
    assert meta["indicators"]["inst_net_buy"] is None
    assert meta["indicators"]["supply_evidence"]["foreign"]["source_date"] is None
    # 실행 소비자가 바꿔도 계획 원문은 보존된다.
    meta["indicators"]["supply_evidence"]["foreign"]["value"] = 55
    assert meta["entry_plan"]["inputs_ref"]["indicator_snapshot"]["values"]["supply_evidence"]["foreign"]["value"] == 0


@pytest.mark.parametrize("indicators", [{}, {"foreign_net_buy": None, "inst_net_buy": 0}])
def test_empty_and_nullable_new_snapshot_never_uses_cache(analyzer, monkeypatch, indicators):
    analyzer._pending = [_pending(analyzer, indicators)]
    analyzer._save_json()
    meta = _execute(analyzer, monkeypatch)
    assert meta["indicators"] == indicators
    assert meta["indicators_source"] == "pending_snapshot"


@pytest.mark.parametrize("broken", [None, [], {}, {"version": 99, "values": {}},
                                      {"version": 1, "values": None},
                                      {"version": 1, "values": []}])
def test_corrupt_snapshot_does_not_silently_replace_evidence(analyzer, monkeypatch, broken):
    row = _pending(analyzer, _indicators()).to_dict()
    row["inputs_ref"]["indicator_snapshot"] = broken
    analyzer._signals_path.write_text(json.dumps([row]))
    meta = _execute(analyzer, monkeypatch)
    assert meta["indicators"] == {}
    assert meta["indicators_source"] == "invalid_snapshot"
    assert meta["indicators_as_of"] is None


def test_old_json_retains_explicit_legacy_cache_origin(analyzer, monkeypatch):
    row = _pending(analyzer, _indicators()).to_dict()
    row["inputs_ref"].pop("indicator_snapshot", None)
    row["inputs_ref"]["indicators_as_of"] = "2026-09-01T10:00:00"
    analyzer._signals_path.write_text(json.dumps([row]))
    meta = _execute(analyzer, monkeypatch)
    assert meta["indicators"] == {"mrs": -99, "foreign_net_buy": 999}
    assert meta["indicators_source"] == "legacy_cache"
    assert meta["indicators_as_of"] == CALCULATED
    assert meta["entry_plan"]["inputs_ref"]["indicators_as_of"] == "2026-09-01T10:00:00"
    meta["indicators"]["mrs"] = 50
    assert analyzer._screener._indicators._cache[SYM]["mrs"] == -99


def test_unknown_time_is_not_replaced_by_execution_time(analyzer, monkeypatch):
    analyzer._screener._indicators._cache_ts.clear()
    analyzer._pending = [_pending(analyzer, _indicators())]
    analyzer._save_json()
    analyzer._screener._indicators._cache_ts[SYM] = NOW
    meta = _execute(analyzer, monkeypatch)
    assert meta["indicators_as_of"] is None
    assert meta["indicators"]["supply_evidence"]["foreign"]["captured_at"] == CAPTURED


def test_pending_does_not_alias_deserialized_input(analyzer):
    row = _pending(analyzer, _indicators()).to_dict()
    before = deepcopy(row)
    restored = PendingSignal.from_dict(row)
    row["inputs_ref"].clear()
    assert restored.to_dict() == before


def test_observed_pair_lci_and_original_dates_survive(analyzer, monkeypatch):
    indicators = _indicators()
    indicators.update(as_of="2026-09-14T15:35:00", lci=-1.25,
                      foreign_net_buy=-15, inst_net_buy=0, supply_data_age=1)
    indicators["supply_evidence"] = {
        "foreign": {"value": -15, "source": "FHKST01010900",
                    "source_date": "2026-09-14", "captured_at": CAPTURED,
                    "status": "observed"},
        "institution": {"value": 0, "source": "FHKST01010900",
                        "source_date": "2026-09-14", "captured_at": CAPTURED,
                        "status": "observed"},
    }
    analyzer._pending = [_pending(analyzer, indicators)]
    analyzer._save_json()
    meta = _execute(analyzer, monkeypatch)
    assert meta["indicators"] == indicators
    assert meta["indicators_as_of"] == "2026-09-14T15:35:00"
    assert meta["indicators"]["lci"] == -1.25
    assert meta["indicators"]["supply_evidence"]["institution"]["value"] == 0


def test_pre_entry_plan_json_without_timestamps_stays_unknown(analyzer, monkeypatch):
    row = _pending(analyzer, _indicators()).to_dict()
    del row["inputs_ref"]
    analyzer._screener._indicators._cache.clear()
    analyzer._screener._indicators._cache_ts.clear()
    analyzer._signals_path.write_text(json.dumps([row]))
    meta = _execute(analyzer, monkeypatch)
    assert meta["indicators"] == {}
    assert meta["indicators_source"] == "legacy_cache"
    assert meta["indicators_as_of"] is None
    assert meta["entry_plan"]["inputs_ref"] == {}


@pytest.mark.parametrize("bad_time", [42, [], "not-an-observation-time"])
def test_invalid_snapshot_time_is_not_promoted(analyzer, monkeypatch, bad_time):
    row = _pending(analyzer, _indicators()).to_dict()
    row["inputs_ref"]["indicator_snapshot"]["as_of"] = bad_time
    analyzer._signals_path.write_text(json.dumps([row]))
    meta = _execute(analyzer, monkeypatch)
    assert meta["indicators"] == {}
    assert meta["indicators_as_of"] is None
    assert meta["indicators_source"] == "invalid_snapshot"


def test_real_technical_and_swing_producers_serialize(analyzer, monkeypatch, tmp_path):
    from pathlib import Path
    import numpy as np
    import src.core.batch_analyzer as bam
    from src.indicators.technical import TechnicalIndicators
    from src.signals.screener.swing_screener import SwingScreener
    from src.strategies.kr.sepa_trend import SEPATrendStrategy
    from test_swing_supply_evidence import candidate, screener, live

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("src.analytics.swing_supply.utc_now",
                        lambda: datetime.fromisoformat("2026-09-15T01:30:00+00:00"))
    monkeypatch.setattr("src.signals.screener.swing_screener.datetime", bam.datetime)
    monkeypatch.setattr("datetime.datetime", bam.datetime)
    monkeypatch.setattr("src.indicators.technical.datetime", bam.datetime)
    ti = TechnicalIndicators()
    bars = [{"date": (NOW - timedelta(days=260-i)).strftime("%Y%m%d"),
             "close": Decimal(10000 + i), "high": Decimal(10270 + i),
             "low": Decimal(9730 + i), "volume": np.int64(10000 + i)}
            for i in range(260)]
    calculated = ti.calculate_all(SYM, bars)
    row = candidate(SYM, **calculated)
    foreign, institution = live(0, SYM), live(100, SYM)
    for raw in (foreign, institution):
        raw["_supply_capture"]["captured_at"] = CAPTURED
    sc, _ = screener(monkeypatch, [foreign], [institution])
    asyncio.run(sc._apply_composite_score([row]))
    SwingScreener._compute_lci_zscore(sc, [row])
    strategy = SEPATrendStrategy()
    strategy.config.min_score = 0
    signals = asyncio.run(strategy.generate_batch_signals([row]))
    assert len(signals) == 1
    analyzer._screener = SimpleNamespace(_indicators=ti)
    analyzer._pending = [analyzer._to_pending_signal(
        signals[0], now=NOW, expires=NOW + timedelta(hours=5), slippage_pct=3)]
    # JSON 허용 기본형으로 생산되며 Decimal/numpy 입력이 문자열로 바뀌지 않는다.
    json.dumps(row.indicators, allow_nan=False)
    analyzer._save_json()
    saved = json.loads(analyzer._signals_path.read_text())
    assert saved[0]["inputs_ref"]["indicator_snapshot"]["values"] == row.indicators
    meta = _execute(analyzer, monkeypatch)
    assert meta["indicators"] == row.indicators
    assert meta["indicators"]["foreign_net_buy"] == 0
    assert type(meta["indicators"]["volume"]) is int
    assert isinstance(meta["indicators"]["close"], float)


@pytest.mark.parametrize("as_of", [None, "2026-09-14T15:30:00"])
def test_real_vcp_producer_preserves_indicators_and_chase_gate(
        analyzer, monkeypatch, tmp_path, as_of):
    from pathlib import Path
    import src.core.batch_analyzer as bam
    from src.core.cross_validator import CrossStrategyValidator
    from src.signals.screener.swing_screener import SwingCandidate

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("datetime.datetime", bam.datetime)
    indicators = {"atr_14": 3.0, "change_1d": 7.0,
                  "breakout_trigger": 10000, "vcp_score": 70}
    if as_of is not None:
        indicators["as_of"] = as_of
    expected = deepcopy(indicators)
    candidate = SwingCandidate(
        SYM, "합성VCP", "vcp_breakout", 70,
        Decimal("10000"), Decimal("9500"), Decimal("11000"), indicators)
    generated = analyzer._generate_vcp_signals([candidate])
    assert len(generated) == 1
    pending = analyzer._to_pending_signal(
        generated[0], now=NOW, expires=NOW + timedelta(hours=5), slippage_pct=3)
    analyzer._pending = [pending]
    # 이후 후보 또는 공용 캐시의 갱신은 대기 근거를 바꾸면 안 된다.
    candidate.indicators["change_1d"] = 0
    analyzer._screener._indicators._cache[SYM] = {"atr_14": 3.0, "change_1d": 0}
    analyzer._screener._indicators._cache_ts[SYM] = bam.datetime.now()
    analyzer._save_json()
    analyzer._pending = []
    meta = _execute(analyzer, monkeypatch)
    assert meta["indicators"] == expected
    assert meta["indicators_as_of"] == (CALCULATED if as_of is None else as_of)
    assert meta["indicators_source"] == "pending_snapshot"
    assert meta["entry_plan"]["entry_mode"] == "breakout"
    assert meta["entry_plan"]["breakout_trigger"] == 10000
    assert meta["atr_pct"] == 3.0

    # 실제 소비자에서 등락/ATR 추격 감점이 유지되어 70→49, 진입 차단된다.
    validator = CrossStrategyValidator()
    allowed, adjusted, _ = validator.validate(
        SYM, "buy", "vcp_breakout", 70, meta, count_stats=False)
    assert allowed is False
    assert adjusted == 49
    missing = deepcopy(meta)
    missing["indicators"] = {}
    allowed_without_evidence, adjusted_without_evidence, _ = validator.validate(
        SYM, "buy", "vcp_breakout", 70, missing, count_stats=False)
    assert allowed_without_evidence is True
    assert adjusted_without_evidence == 64
