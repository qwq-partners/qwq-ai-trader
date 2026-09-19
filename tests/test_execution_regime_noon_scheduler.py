"""C3 installed noon classifier adapters and scheduler dispatch boundaries.

This file deliberately stops at the scheduler/owner boundary.  The owner's
thirteen-row protection/replay acceptance matrix lives in a separate test
module; these tests catch regressions where the real ``KRScheduler`` bypasses
that owner, eagerly touches external collaborators, or confuses the immediate
application receipt with the independent thirty-minute synchronization.
"""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.schedulers.kr_scheduler import KRScheduler
from test_execution_intraday_owner import batch_for
from test_execution_regime_owner import owned, quote
from test_execution_runtime import NOW


NOON = NOW.replace(hour=12)


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    """Imported fixtures do not carry their module-local synthetic HOME."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def _c3_api():
    # Late imports keep collection useful on the frozen C2 base.  A failure
    # here is a bootstrap failure, not behavioral RED evidence.
    from src.execution.safety.regime_owner import (
        RegimeHorizonBaseline,
        RegimeOwner,
    )
    from src.schedulers.kr_scheduler import _RegimeClassifierInputs

    return RegimeHorizonBaseline, RegimeOwner, _RegimeClassifierInputs


def _observation(code, now, *, change_pct=0.0, price=100.0):
    value = quote(code, pct=change_pct)
    value["price"] = price
    value["_observation"]["received_at"] = now.isoformat()
    value["_observation"]["market_as_of"] = None
    value["_observation"]["fields"]["price"]["value"] = price
    value["_observation"]["fields"]["change_pct"]["value"] = change_pct
    return value


class _Screener:
    def __init__(self, now, *, regime="neutral"):
        self._kospi_closes = [100.0] * 21
        self._kospi_last_bar_date = now.date()
        self._kospi_loaded_at = now - timedelta(minutes=5)
        self._regime = regime
        self.regime_reads = 0

    def get_market_regime(self):
        self.regime_reads += 1
        return self._regime


class _Overnight:
    def __init__(self, now, calls):
        self._now = now
        self._calls = calls

    async def get_overnight_signal(self):
        self._calls.append("us")
        common = {"missing": False, "fetched_at": self._now.isoformat(), "as_of": None}
        return {
            "indices": {},
            "indices_normalized": {
                "SP500": {**common, "change_pct": 0.0},
                "NASDAQ": {**common, "change_pct": 0.0},
                "SOX": {**common, "change_pct": 0.0},
                "VIX": {**common, "price": 20.0},
            },
        }


class _QuickLLM:
    def __init__(self, calls):
        self.calls = calls

    async def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "regime": "ranging",
            "lead_strategy": "balanced",
            "sepa_min_score_today": 65,
            "rsi2_min_score_today": 60,
            "entry_start_time": "09:01",
            "confidence": 0.75,
            "reasoning": "synthetic scheduler acceptance",
        }


def _patch_us(monkeypatch, overnight):
    import src.data.providers.us_market_data as us_market_data

    monkeypatch.setattr(us_market_data, "get_us_market_data", lambda: overnight)


def _patch_llm_manager(monkeypatch, factory):
    import src.schedulers.kr_scheduler as scheduler_module
    import src.utils.llm as llm_module

    monkeypatch.setattr(llm_module, "get_llm_manager", factory)
    # Accept either a local late import or a module-level alias without making
    # the test prescribe which of those two implementation details is used.
    monkeypatch.setattr(scheduler_module, "get_llm_manager", factory, raising=False)


def _write_daily_bias(tmp_path, value=None):
    path = tmp_path / ".cache" / "ai_trader" / "daily_bias.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value or {"assessment": 0, "top_lesson": ""}), encoding="utf-8")
    return path


def _provider_scheduler(*, engine=None, batch=None, kis=None, guard=True, exits=None):
    scheduler = object.__new__(KRScheduler)
    scheduler.bot = SimpleNamespace(
        engine=engine,
        batch_analyzer=batch,
        kis_market_data=kis,
        exit_manager=exits,
        config={"kr": {"llm_ops": {"regime_conflict_guard_enabled": guard}}},
    )
    return scheduler


async def _owned_c3(tmp_path, monkeypatch, clock):
    """Extend the real C2 SQLite owner with the explicit public C3 baseline."""
    RegimeHorizonBaseline, RegimeOwner, _ = _c3_api()
    engine, exits, store, runtime, owner, _ = await owned(
        tmp_path, clock=lambda: clock[0]
    )

    # Use the actual intraday writer for the current source fact.  No source or
    # application ledger row is manufactured by this fixture.
    from src.execution.safety.intraday_owner import IntradayRiskOwner

    batch = batch_for(engine, exits)
    intraday = IntradayRiskOwner(runtime, batch)

    class CurrentIndex:
        async def fetch_index_price(self, code):
            assert code == "0001"
            return _observation(code, clock[0])

    current = await intraday.refresh(CurrentIndex())
    assert current.status == "accepted"

    state = runtime.owner.state
    baseline = RegimeHorizonBaseline.from_dict(
        {
            "schema": 1,
            "baseline_id": "independent-c3-scheduler-horizon",
            "account_scope": "scope",
            "business_day": clock[0].date().isoformat(),
            "generation": runtime._day_generation,
            "fence_id": runtime._day_fence_id,
            "evidence": {
                "source": "independent-scheduler-acceptance",
                "event_id": "known-horizon",
                "observed_at": clock[0].isoformat(),
            },
            "regime_baseline_version": state["regime_policy"]["baseline"]["baseline_version"],
            "intraday": {
                "baseline_version": state["intraday_policy"]["baseline_version"],
                "current": state["intraday_policy"]["current"],
            },
            "horizon": {"level": "normal", "change_pct": 0.0, "classified_at": None},
        }
    )
    await RegimeOwner.register_horizon_baseline(
        runtime, baseline, expected_version=runtime.owner.version
    )
    await runtime.owner.register_policy_generations(
        "independent-c3-scheduler-reads",
        (
            "regime_policy.horizon",
            "intraday_policy.current",
            "protection.config",
            "protection.current_regime",
            "protection.intraday_crash_level",
        ),
    )
    return engine, exits, store, runtime, owner, batch


def _installed_scheduler(tmp_path, monkeypatch, clock):
    async def build():
        engine, exits, store, runtime, owner, batch = await _owned_c3(
            tmp_path, monkeypatch, clock
        )
        screener = _Screener(clock[0])
        batch._screener = screener
        calls = []

        class IndexProvider:
            async def fetch_index_price(self, code):
                calls.append(code)
                return _observation(code, clock[0])

        scheduler = _provider_scheduler(
            engine=engine,
            batch=batch,
            kis=IndexProvider(),
            guard=True,
            exits=exits,
        )
        scheduler.bot.risk_manager = owner.sidecar
        scheduler.bot.expert_orchestrator = None
        return scheduler, engine, exits, store, runtime, owner, batch, screener, calls

    return build()


def test_classifier_provider_construction_has_no_file_singleton_screener_or_kis_side_effect(
    tmp_path, monkeypatch
):
    """Catches moving live input capture into provider construction/binding."""
    _, _, Inputs = _c3_api()
    touched = []

    class BombScreener:
        def __getattribute__(self, name):
            if name.startswith("_kospi") or name == "get_market_regime":
                touched.append("screener")
                raise AssertionError("screener read during provider construction")
            return object.__getattribute__(self, name)

    class BombKIS:
        async def fetch_index_price(self, code):
            touched.append("kis")
            raise AssertionError("KIS read during provider construction")

    _patch_us(monkeypatch, SimpleNamespace(get_overnight_signal=lambda: touched.append("us")))
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("file read during provider construction")
    ))
    scheduler = _provider_scheduler(
        batch=SimpleNamespace(_screener=BombScreener()), kis=BombKIS()
    )

    provider = Inputs(scheduler)

    assert provider is not None
    assert touched == []


def test_classifier_provider_preserves_missing_legitimate_zero_and_unknown_market_time(
    tmp_path, monkeypatch
):
    """Catches ``value or default`` and receipt-time-as-market-time regressions."""
    _, _, Inputs = _c3_api()
    calls = []
    clock = [NOON]
    screener = _Screener(clock[0])
    overnight = _Overnight(clock[0], calls)
    _patch_us(monkeypatch, overnight)

    class IndexProvider:
        async def fetch_index_price(self, code):
            calls.append(code)
            return _observation(code, clock[0], change_pct=0.0)

    scheduler = _provider_scheduler(
        batch=SimpleNamespace(_screener=screener), kis=IndexProvider()
    )
    provider = Inputs(scheduler)

    missing = asyncio.run(provider.read_daily_bias()).to_dict()
    assert missing["outcome"] == "missing" and missing["payload"] is None

    _write_daily_bias(tmp_path, {"assessment": 0, "top_lesson": ""})
    daily = asyncio.run(provider.read_daily_bias()).to_dict()
    us = asyncio.run(provider.fetch_us_overnight()).to_dict()
    screen = provider.snapshot_screener().to_dict()
    index = asyncio.run(provider.fetch_index_price("0001")).to_dict()

    assert daily["outcome"] == "success"
    assert daily["payload"]["data"]["assessment"] == 0
    assert us["payload"]["overnight"]["indices_normalized"]["SP500"]["change_pct"] == 0.0
    assert screen["payload"]["closes"] == [100.0] * 21
    assert index["payload"]["quote"]["change_pct"] == 0.0
    assert index["payload"]["quote"]["_observation"]["market_as_of"] is None
    assert calls == ["us", "0001"]


def test_sync_context_guard_false_never_reads_screener(tmp_path):
    """Catches inventing a policy dependency on a disabled conflict guard."""
    _, _, Inputs = _c3_api()

    class BombScreener:
        def get_market_regime(self):
            raise AssertionError("disabled guard read screener")

    scheduler = _provider_scheduler(
        batch=SimpleNamespace(_screener=BombScreener()), guard=False
    )
    context = Inputs(scheduler).snapshot_application_context(captured_at=NOON).to_dict()

    assert context == {
        "schema": 1,
        "captured_at": NOON.isoformat(),
        "regime_conflict_guard_enabled": False,
        "screener": {
            "regime": None,
            "source": "batch_analyzer._screener.get_market_regime",
            "event_id": None,
            "observed_at": None,
        },
    }


def test_installed_classifier_acquires_llm_only_after_source_admission(
    tmp_path, monkeypatch
):
    """Catches eager global model acquisition before durable classifier begin."""
    async def scenario():
        clock = [NOON]
        scheduler, _, _, store, runtime, owner, _, _, index_calls = await _installed_scheduler(
            tmp_path, monkeypatch, clock
        )
        _write_daily_bias(tmp_path)
        us_calls, model_calls, acquisitions = [], [], []
        _patch_us(monkeypatch, _Overnight(clock[0], us_calls))
        llm = _QuickLLM(model_calls)

        def acquire():
            latest = runtime.owner.state["risk_sources"]["latest"].get("llm_regime")
            assert latest is not None
            row = runtime.owner.state["risk_sources"]["records"][latest]
            assert row["terminal"] is None
            acquisitions.append(latest)
            return llm

        _patch_llm_manager(monkeypatch, acquire)
        try:
            receipt = await scheduler._run_llm_regime_classifier(
                label="12:00 (장중 업데이트)"
            )
            application = owner.classifier_application_receipt(receipt.operation_id)
            assert receipt.status == "accepted"
            assert application is not None
            assert acquisitions == [receipt.operation_id]
            assert len(model_calls) == 1
            assert model_calls[0]["task"].name == "QUICK_ANALYSIS"
            assert "max_tokens" not in model_calls[0]
            assert us_calls == ["us"] and index_calls == ["0001", "1001"]
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_installed_classifier_ignores_legacy_json_and_only_owner_may_apply_protection(
    tmp_path, monkeypatch
):
    """Catches installed fallback to the file-authority/direct-writer branch."""
    async def scenario():
        clock = [NOON]
        scheduler, _, exits, store, runtime, owner, _, _, _ = await _installed_scheduler(
            tmp_path, monkeypatch, clock
        )
        _write_daily_bias(tmp_path)
        _patch_us(monkeypatch, _Overnight(clock[0], []))
        model_calls = []
        _patch_llm_manager(monkeypatch, lambda: _QuickLLM(model_calls))
        legacy = tmp_path / ".cache" / "ai_trader" / "llm_regime_today.json"
        legacy.write_text(json.dumps({
            "date": clock[0].date().isoformat(), "regime": "trending_bull"
        }), encoding="utf-8")

        owner_depth = [0]
        original_classify = owner.classify
        original_apply = exits.apply_regime_params

        async def classify(*args, **kwargs):
            owner_depth[0] += 1
            try:
                return await original_classify(*args, **kwargs)
            finally:
                owner_depth[0] -= 1

        def guarded_apply(*args, **kwargs):
            assert owner_depth[0] > 0, "scheduler called ExitManager directly"
            return original_apply(*args, **kwargs)

        monkeypatch.setattr(owner, "classify", classify)
        monkeypatch.setattr(exits, "apply_regime_params", guarded_apply)
        try:
            receipt = await scheduler._run_llm_regime_classifier(
                label="12:00 (장중 업데이트)"
            )
            application = owner.classifier_application_receipt(receipt.operation_id)
            assert receipt.status == "accepted" and application is not None
            assert exits._current_regime == "ranging"
            assert len(model_calls) == 1
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_installed_thirty_minute_sync_uses_fresh_context_without_new_source_or_model(
    tmp_path, monkeypatch
):
    """Catches implementing periodic sync as classifier/file replay."""
    async def scenario():
        clock = [NOON]
        scheduler, _, _, store, runtime, owner, _, screener, _ = await _installed_scheduler(
            tmp_path, monkeypatch, clock
        )
        _write_daily_bias(tmp_path)
        _patch_us(monkeypatch, _Overnight(clock[0], []))
        model_calls = []
        _patch_llm_manager(monkeypatch, lambda: _QuickLLM(model_calls))
        try:
            source = await scheduler._run_llm_regime_classifier(
                label="12:00 (장중 업데이트)"
            )
            assert source.status == "accepted"
            source_records = set(runtime.owner.state["risk_sources"]["records"])
            model_count = len(model_calls)
            captured = []
            original_sync = owner.sync_protection

            async def sync(operation_id, *, supplied_context):
                captured.append((operation_id, supplied_context.to_dict()))
                return await original_sync(
                    operation_id, supplied_context=supplied_context
                )

            monkeypatch.setattr(owner, "sync_protection", sync)
            _patch_llm_manager(monkeypatch, lambda: (_ for _ in ()).throw(
                AssertionError("periodic sync acquired a model")
            ))
            prior_regime_reads = screener.regime_reads
            clock[0] += timedelta(seconds=1)
            result = await scheduler._apply_regime_to_exit_manager()

            assert result is not None
            assert captured[0][0].startswith("regime-sync:")
            assert captured[0][1]["captured_at"] == clock[0].isoformat()
            assert captured[0][1]["screener"]["regime"] == "neutral"
            assert set(runtime.owner.state["risk_sources"]["records"]) == source_records
            assert len(model_calls) == model_count
            assert screener.regime_reads == prior_regime_reads + 1
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_noon_loop_reads_immediate_application_then_runs_one_independent_periodic_sync(
    tmp_path, monkeypatch
):
    """Catches a second model/application dispatch in the actual batch loop."""
    async def scenario():
        import src.schedulers.kr_scheduler as scheduler_module

        clock = [NOON]
        scheduler, _, _, store, runtime, owner, batch, _, _ = await _installed_scheduler(
            tmp_path, monkeypatch, clock
        )
        _write_daily_bias(tmp_path)
        _patch_us(monkeypatch, _Overnight(clock[0], []))
        model_calls = []
        _patch_llm_manager(monkeypatch, lambda: _QuickLLM(model_calls))

        classifier_calls, immediate_reads, sync_calls = [], [], []
        owner_depth = [0]
        original_classify = owner.classify
        original_read = owner.classifier_application_receipt
        original_sync = owner.sync_protection

        async def classify(*args, **kwargs):
            owner_depth[0] += 1
            try:
                result = await original_classify(*args, **kwargs)
                classifier_calls.append(result.operation_id)
                return result
            finally:
                owner_depth[0] -= 1

        def read(operation_id):
            if owner_depth[0] == 0:
                immediate_reads.append(operation_id)
            return original_read(operation_id)

        async def sync(operation_id, *, supplied_context):
            sync_calls.append(operation_id)
            owner_depth[0] += 1
            try:
                return await original_sync(
                    operation_id, supplied_context=supplied_context
                )
            finally:
                owner_depth[0] -= 1

        monkeypatch.setattr(owner, "classify", classify)
        monkeypatch.setattr(owner, "classifier_application_receipt", read)
        monkeypatch.setattr(owner, "sync_protection", sync)
        monkeypatch.setattr(scheduler_module, "is_kr_market_holiday", lambda day: False)
        monkeypatch.setattr(scheduler_module.time, "time", lambda: 3600.0)

        async def no_intraday_refresh():
            return None

        async def monitor_positions():
            return None

        monkeypatch.setattr(scheduler, "_refresh_intraday_risk", no_intraday_refresh)
        batch.monitor_positions = monitor_positions
        scheduler.bot.running = True
        scheduler.bot.config["kr"]["batch"] = {
            "morning_scan_enabled": False,
            "daily_scan_time": "15:40",
            "execute_time": "09:01",
            "position_update_interval": 30,
            "lunchtime_scan": {"enabled": False},
        }
        scheduler.bot.config["kr"]["llm_ops"].update({
            "regime_classifier_enabled": True,
            "position_eod_check_enabled": False,
            "false_negative_analysis_enabled": False,
        })
        real_sleep = asyncio.sleep

        async def one_tick(seconds):
            if seconds == 30:
                scheduler.bot.running = False
            await real_sleep(0)

        monkeypatch.setattr(scheduler_module.asyncio, "sleep", one_tick)
        try:
            await scheduler.run_batch_scheduler()
            assert len(classifier_calls) == 1
            assert immediate_reads == classifier_calls
            assert len(sync_calls) == 1
            assert sync_calls[0].startswith("regime-sync:")
            assert sync_calls[0] != "classifier:" + classifier_calls[0]
            assert len(model_calls) == 1
        finally:
            await runtime.shutdown()
            await store.close()

    asyncio.run(scenario())
