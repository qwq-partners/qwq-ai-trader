"""Synthetic one-cycle execution of the actual scheduler, with no runtime I/O."""
import ast
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import re
from types import SimpleNamespace as NS

import pytest
from loguru import logger
from src.core.event import SignalEvent
from src.core.types import MarketSession, Signal, OrderSide, SignalStrength, StrategyType
from src.analytics.entry_observation import capture_scan, capture_rest_quote, emit_with_observation
from src.analytics.entry_gate_trace import safe_trace_call, begin_gate_trace, NULL_TRACE, STAGES
from src.analytics.entry_observation import EntryObservationBuffer
from src.utils.sizing import atr_position_multiplier

ROOT = Path(__file__).resolve().parents[1]


class RecordingTrace:
    def __init__(self):
        self.calls = []
    def __getattr__(self, method):
        def record(*args, **kwargs):
            self.calls.append((method, args, kwargs))
        return record


class NoDisk:
    """Replace the scheduler's existing cache reads/writes, never actual paths."""
    @classmethod
    def home(cls): return cls()
    def __truediv__(self, part): return self
    def exists(self): return False
    @property
    def parent(self): return self
    def mkdir(self, **kwargs): pass
    def write_text(self, value): pass


def stock(symbol="S", **kwargs):
    return NS(**(dict(symbol=symbol, name="synthetic", score=90, price=10000,
                     change_pct=2, reasons=[], atr_pct=4, volume_ratio=3) | kwargs))


def run_cycle(monkeypatch, *, stocks=None, trace=None, enabled=None, quote=None,
              cash=1000000, positions=None, cooldown=None, daily=None,
              sector=None, validator=None, emit_error=False, cancel_quote=False,
              regime=0, hour=10, minute=30, session=MarketSession.REGULAR,
              engine_present=True, broker_present=True, safe=None, observer=None,
              actual_trace=False, before_emit=None, momentum_start="09:15", overnight=None):
    stocks = stocks if stocks is not None else [stock()]
    trace = trace if trace is not None else RecordingTrace()
    calls, events = [], []
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 2, hour, minute, tzinfo=tz)
    async def screen_all(**kwargs):
        calls.append(("screen", kwargs)); return stocks
    async def night(): calls.append(("night",)); return None
    async def get_quote(symbol):
        calls.append(("quote", symbol))
        if symbol in ("069500", "229200"):
            if isinstance(regime, Exception): raise regime
            if isinstance(regime, tuple): return regime[0 if symbol == "069500" else 1]
            return regime if isinstance(regime, dict) else {"change_pct": regime}
        if cancel_quote: raise asyncio.CancelledError()
        result = quote if quote is not None else {"price":10000,"change_pct":2,"open":9900,"volume":100}
        if isinstance(result, Exception): raise result
        return result
    async def emit(event):
        calls.append(("emit", event.symbol)); events.append(event)
        if before_emit is not None: before_emit(event)
        if isinstance(emit_error, BaseException): raise emit_error
        if emit_error: raise RuntimeError("synthetic emit")
    async def get_sector(symbol):
        calls.append(("sector", symbol))
        if isinstance(sector, Exception): raise sector
        return sector
    async def validate(**kwargs):
        calls.append(("validate", kwargs["symbol"]))
        if isinstance(validator, Exception): raise validator
        if isinstance(validator, NS): return validator
        return NS(approved=validator, confidence_adjustment=0.05, block_reason="synthetic")
    async def siglog(**kwargs): pass
    bot = NS(running=True,screener=NS(screen_all=screen_all),theme_detector=None,
             kis_market_data=NS(get_night_futures_quote=night),
             _watch_symbols_lock=asyncio.Lock(), _watch_symbols=[], _screening_interval=60,
             strategy_manager=NS(enabled_strategies=["sepa_trend"] if enabled is None else enabled),
             config=NS(get=lambda *a: {"trading_start_time":momentum_start} if a[-1]=="momentum_breakout" else {}), _entry_price_observer=observer,
             engine=NS(portfolio=NS(positions=positions or {}), risk_manager=NS(_pending_orders=[]),
                       config=NS(risk=NS(min_position_value=200000,max_positions_per_sector=1)),
                       get_available_cash=lambda:cash, emit=emit),
             broker=NS(get_quote=get_quote),risk_manager=NS(_stop_loss_today=[]),
             _screening_signal_cooldown=dict(cooldown or {}),_daily_entry_count=dict(daily or {}),
             _stock_validator=NS(validate=validate) if validator is not None else None,
             _get_sector=get_sector)
    if overnight is not None:
        async def overnight_signal(): return overnight
        bot.us_market_data=NS(get_overnight_signal=overnight_signal)
    if not engine_present: bot.engine = None
    if not broker_present: bot.broker = None
    async def sleep(delay):
        calls.append(("sleep",delay))
        if delay == 60 and sum(c == ("sleep",60) for c in calls) > 1: bot.running=False
    scope=dict(asyncio=NS(sleep=sleep,gather=asyncio.gather,create_task=asyncio.create_task,
                         CancelledError=asyncio.CancelledError),datetime=Clock,timezone=timezone,
               timedelta=timedelta,Decimal=Decimal,Path=NoDisk,json=json,re=re,math=math,logger=logger,
               MarketSession=MarketSession,Signal=Signal,SignalEvent=SignalEvent,OrderSide=OrderSide,
               SignalStrength=SignalStrength,StrategyType=StrategyType,atr_position_multiplier=atr_position_multiplier,
               capture_scan=capture_scan,capture_rest_quote=capture_rest_quote,emit_with_observation=emit_with_observation,
               observe_screen_candidates=lambda *a:None,begin_gate_trace=begin_gate_trace if actual_trace else lambda *a:trace,
               safe_trace_call=safe or safe_trace_call,
               _hb=NS(**{k:lambda *a,**kw:None for k in ("record_attempt","record_idle","record_success","record_failure")}),
               trading_logger=NS(log_screening=lambda **kw:None),_SigLog=NS(get=lambda:NS(log=siglog)))
    tree=ast.parse((ROOT/"src/schedulers/kr_scheduler.py").read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=="KRScheduler")
    method=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="run_screening")
    exec(compile(ast.Module(body=[method],type_ignores=[]),"actual_scheduler","exec"),scope)
    asyncio.run(scope["run_screening"](NS(bot=bot,_get_current_session=lambda:session)))
    return trace,calls,events,bot


def checks(trace, symbol="S"):
    return {a[0]: (a[1], kw) for method,a,kw in trace.calls
            if method == "check" and kw.get("symbol", symbol) == symbol}


def test_disabled_strategy_records_first_global_rejection(monkeypatch):
    trace,calls,events,bot=run_cycle(monkeypatch,enabled=[])
    assert checks(trace)["enabled"][0] is False
    assert "session" not in checks(trace)
    assert not events
    assert any(method == "finish" for method,_,_ in trace.calls)


def test_success_records_real_created_event_before_emit(monkeypatch):
    trace,calls,events,bot=run_cycle(monkeypatch)
    assert len(events) == 1
    signals=[args for method,args,_ in trace.calls if method == "signal"]
    assert signals == [("S",events[0].id)]
    assert bot._daily_entry_count == {"S":1}
    assert checks(trace)["score"][0] is True


@pytest.mark.parametrize("kwargs,stage", [
    ({"cash":0},"cash"),({"stocks":[stock(score=74)]},"score"),
    ({"positions":{"S":NS(sector="x")}},"excluded"),
    ({"cooldown":{"S":datetime(2026,10,2,10,29)}},"cooldown"),
    ({"daily":{"S":2}},"daily_count"),({"stocks":[stock(change_pct=8)]},"scan_change"),
    ({"enabled":["rsi2_reversal"]},"strategy"),
    ({"quote":{"price":0}},"quote_price"),
    ({"quote":{"price":10000,"change_pct":0}},"rt_min_change"),
    ({"quote":{"price":10000,"change_pct":16}},"rt_max_change"),
    ({"quote":{"price":10000,"change_pct":2,"open":11000}},"open_price"),
    ({"quote":{"price":10000,"change_pct":2,"volume":0}},"volume"),
    ({"validator":False},"news"),({"stocks":[stock(atr_pct=11)]},"atr"),
    ({"stocks":[stock(atr_pct=1)]},"chase"),
    ({"stocks":[stock(atr_pct=float("nan"))]},"risk_reward"),
    ({"regime":-2},"regime"),({"hour":9,"minute":14},"entry_time"),
    ({"engine_present":False},"engine"),({"broker_present":False},"broker"),
    ({"session":MarketSession.PRE_MARKET},"session"),
    ({"positions":{"OTHER":NS(sector="x")},"sector":"x"},"sector"),
    ({"enabled":["momentum_breakout"],"stocks":[stock(volume_ratio=0)]},"momentum_volume"),
    ({"enabled":["momentum_breakout"]},"momentum_strength"),
    ({"enabled":["momentum_breakout"],"stocks":[stock(reasons=["MA20+3%","RSI:80"])]},"rsi"),
    ({"enabled":["momentum_breakout"],"stocks":[stock(score=80,reasons=["MA20+3%"])]},"early_supply"),
])
def test_first_rejection_uses_actual_branch(monkeypatch,kwargs,stage):
    trace,calls,events,bot=run_cycle(monkeypatch,**kwargs)
    failed=[name for name,(result,_) in checks(trace).items() if result is False]
    assert failed == [stage]
    assert list(checks(trace))[-1] == stage
    assert not events


def observer_buffer(**kwargs):
    return EntryObservationBuffer(evaluation_epoch="synthetic-v3",capacity=kwargs.pop("capacity",100),
        scan_scope="first",scan_admission_ref="synthetic",entry_gate_trace_settings={
            "version":"entry-gate-trace-v1","policy_ref":"kr-live-screening-gates-v1",
            "max_candidates":100,"source_version_ref":"synthetic-source",
            "configuration_ref":"synthetic-config"},**kwargs)


def rows(observer):
    return {r["symbol"]:r for r in observer.export()["records"] if r["kind"]=="entry_gate_trace"}


@pytest.mark.parametrize("kwargs", [
    {}, {"enabled":[]}, {"cash":0}, {"quote":RuntimeError("synthetic")},
    {"emit_error":True}, {"cancel_quote":True}, {"validator":RuntimeError("synthetic")},
    {"stocks":[stock(str(i)) for i in range(10)]},
    {"enabled":["momentum_breakout"],"stocks":[stock(reasons=["MA20+3%","RSI:70","기관 순매수"])]},
])
def test_trace_failure_cannot_change_calls_events_or_counters(monkeypatch,kwargs):
    class ThrowingTrace:
        def __getattr__(self,method):
            def fail(*args,**kw): raise RuntimeError("synthetic trace failure")
            return fail
    results=[]
    for trace in (NULL_TRACE,RecordingTrace(),ThrowingTrace(),"actual"):
        options={"observer":observer_buffer(),"actual_trace":True} if trace == "actual" else {"trace":trace}
        _,calls,events,bot=run_cycle(monkeypatch,**options,**kwargs)
        event_fields=[]
        for event in events:
            fields=asdict(event)
            fields.pop("id"); fields.pop("timestamp")
            fields["signal"].pop("timestamp",None)
            event_fields.append(fields)
        results.append((calls,event_fields,bot._daily_entry_count,bot._screening_signal_cooldown))
    assert results[0] == results[1] == results[2] == results[3]


def test_actual_trace_covers_all_stages_and_keeps_strategy_exemptions(monkeypatch):
    observer=observer_buffer()
    _,_,events,_=run_cycle(monkeypatch,observer=observer,actual_trace=True)
    row=rows(observer)["S"]
    assert row["outcome"]=="signal_created"
    assert row["signal_id"]==events[0].id
    assert [s["stage"] for s in row["steps"]] == list(STAGES)
    assert {s["stage"] for s in row["steps"] if s["status"]=="not_applicable"} == {
        "sector","momentum_volume","momentum_strength","rsi","early_supply","news"}
    assert observer.export()["complete"] is True


def test_eight_candidate_and_five_signal_limits_keep_full_denominator(monkeypatch):
    observer=observer_buffer()
    _,calls,events,bot=run_cycle(monkeypatch,stocks=[stock(str(i)) for i in range(10)],observer=observer,actual_trace=True)
    result=rows(observer)
    assert len(result)==10
    assert [r["outcome"] for r in result.values()] == ["signal_created"]*5+["not_reached"]*5
    assert [result[str(i)]["terminal_reason"] for i in range(5,10)] == ["signal_limit"]*3+["batch_limit"]*2
    assert [e.symbol for e in events]==["0","1","2","3","4"]
    assert [c[1] for c in calls if c[0]=="quote"]==["069500","229200","0","1","2","3","4"]


def test_batch_limit_after_eight_actual_rejections(monkeypatch):
    observer=observer_buffer()
    run_cycle(monkeypatch,stocks=[stock(str(i)) for i in range(9)],quote={"price":0},observer=observer,actual_trace=True)
    result=rows(observer)
    assert [r["outcome"] for r in result.values()]==["blocked"]*8+["not_reached"]
    assert result["8"]["terminal_reason"]=="batch_limit"


def test_signal_is_recorded_before_emit_and_failure_does_not_claim_emit_success(monkeypatch):
    observer=observer_buffer()
    def check_created(event):
        # Finish is deferred, so inspect actual in-flight trace via event creation hook below.
        assert event.id
    trace=RecordingTrace()
    def check_trace(event):
        assert ("signal",("S",event.id),{}) in trace.calls
    run_cycle(monkeypatch,trace=trace,before_emit=check_trace,emit_error=True)
    _,_,events,bot=run_cycle(monkeypatch,stocks=[stock("S"),stock("T")],observer=observer,
                            actual_trace=True,emit_error=True,before_emit=check_created)
    result=rows(observer)
    assert result["S"]["outcome"]=="signal_created"
    assert result["S"]["signal_id"]==events[0].id
    assert result["T"]["outcome"]=="not_reached"
    assert result["T"]["terminal_reason"]=="emit_break"
    assert bot._daily_entry_count=={}
    emitted=[r for r in observer.export()["records"] if r["kind"]=="emit_result"]
    assert emitted[0]["emitted"] is False


def test_cancel_preserves_partial_unknown_and_original_swallow_behavior(monkeypatch):
    observer=observer_buffer()
    _,_,events,bot=run_cycle(monkeypatch,stocks=[stock("S"),stock("T")],observer=observer,actual_trace=True,cancel_quote=True)
    assert not events and not bot._daily_entry_count
    result=rows(observer)
    assert {r["outcome"] for r in result.values()}=={"unknown"}
    assert result["S"]["steps"][-1]["stage"]=="sector"
    assert result["T"]["steps"][-1]["stage"]=="batch_limit"
    assert {r["terminal_reason"] for r in result.values()}=={"scope_interrupted"}


@pytest.mark.parametrize("kwargs,stage,status", [
    ({"quote":RuntimeError("synthetic")},"quote_fetch","unknown"),
    ({"validator":RuntimeError("synthetic")},"news","unknown"),
    ({"sector":RuntimeError("synthetic")},"sector","unknown"),
    ({"regime":RuntimeError("synthetic")},"regime","unknown"),
    ({"enabled":["momentum_breakout"],"stocks":[stock(reasons=["MA20+3%"])]},"rsi","not_applicable"),
])
def test_fallbacks_are_not_promoted_to_verified_pass(monkeypatch,kwargs,stage,status):
    observer=observer_buffer()
    run_cycle(monkeypatch,observer=observer,actual_trace=True,**kwargs)
    row=rows(observer)["S"]
    assert next(s["status"] for s in row["steps"] if s["stage"]==stage)==status
    assert row["outcome"] == ("unknown" if stage=="quote_fetch" else "signal_created")


@pytest.mark.parametrize("kwargs,stage,expected", [
    ({"stocks":[stock(score=float("nan"))]},"score",False),
    ({"stocks":[stock(change_pct=float("nan"))]},"scan_change",False),
    ({"quote":{"price":10000,"change_pct":float("nan"),"open":9900,"volume":100}},"rt_min_change",True),
    ({"stocks":[stock(atr_pct=float("nan"))]},"atr",True),
])
def test_nan_preserves_original_comparator_polarity(monkeypatch,kwargs,stage,expected):
    trace,_,_,_=run_cycle(monkeypatch,**kwargs)
    assert checks(trace)[stage][0] is expected


def test_prefilter_short_circuit_does_not_read_later_daily_count(monkeypatch):
    # The score failure must prevent exclusion/cooldown/count/change observations.
    trace,_,_,_=run_cycle(monkeypatch,stocks=[stock(score=74,change_pct=99)],daily={"S":2})
    assert [a[0] for method,a,_ in trace.calls if method=="check"] == [
        "enabled","session","engine","broker","entry_time","regime","cash","score"]


def test_effective_dynamic_thresholds_and_strategy_start_are_copied(monkeypatch):
    trace,_,events,_=run_cycle(monkeypatch,stocks=[stock(score=87)],overnight={
        "sentiment":"bearish","indices":{"a":{"change_pct":3}}})
    assert checks(trace)["score"][1]["threshold"]==90
    assert not events
    trace,_,events,_=run_cycle(monkeypatch,enabled=["momentum_breakout"],momentum_start="11:00")
    assert checks(trace)["strategy"][0] is False
    assert not events


def test_quote_value_and_atr_fallback_are_observed_without_extra_reads(monkeypatch):
    trace,_,_,_=run_cycle(monkeypatch,stocks=[stock(atr_pct=None)])
    assert checks(trace)["quote_price"][1]["value"] == 10000
    assert checks(trace)["atr"][1]["value"] == 4.0
    assert checks(trace)["atr"][1]["reason"] == "default_fallback"


@pytest.mark.parametrize("atr,reasons,value,reason", [
    (0,["ATR: 3%)"],3.0,"reason_fallback"),
    (0,[],4.0,"default_fallback"),
    (4,[],4,"screened_atr"),
])
def test_atr_records_effective_source(monkeypatch,atr,reasons,value,reason):
    trace,_,_,_=run_cycle(monkeypatch,stocks=[stock(atr_pct=atr,reasons=reasons)])
    assert checks(trace)["atr"][1]["value"] == value
    assert checks(trace)["atr"][1]["reason"] == reason


def test_observer_overflow_cannot_suppress_the_actual_signal(monkeypatch):
    observer=observer_buffer(capacity=1)
    _,_,events,bot=run_cycle(monkeypatch,observer=observer,actual_trace=True)
    assert len(events)==1 and bot._daily_entry_count=={"S":1}
    assert observer.export()["complete"] is False
    assert len(observer.export()["records"])==1


def test_window_close_during_emit_cannot_suppress_counter_or_following_signal(monkeypatch):
    observer=observer_buffer()
    def close_capture(event):
        observer.mark_incomplete("synthetic_window_end")
        observer._capture_closed=True
    _,_,events,bot=run_cycle(monkeypatch,stocks=[stock("S"),stock("T")],observer=observer,
                            actual_trace=True,before_emit=close_capture)
    assert [event.symbol for event in events]==["S","T"]
    assert bot._daily_entry_count=={"S":1,"T":1}
    assert not rows(observer)
    assert observer.export()["complete"] is False


@pytest.mark.parametrize("legacy", [None,"selection-basis-v1","selection-basis-v2"])
def test_unavailable_and_legacy_trace_add_no_gate_records(monkeypatch,legacy):
    observer=EntryObservationBuffer(evaluation_epoch="synthetic-legacy",capacity=100,
        selection_basis_settings={"version":legacy,"max_candidates":100,"max_source_terms":16} if legacy else None)
    _,_,events,bot=run_cycle(monkeypatch,observer=observer,actual_trace=True)
    assert len(events)==1 and bot._daily_entry_count=={"S":1}
    assert not rows(observer)


def test_failure_after_news_approval_does_not_duplicate_the_observed_gate(monkeypatch):
    observer=observer_buffer()
    _,_,events,_=run_cycle(monkeypatch,observer=observer,actual_trace=True,
                          validator=NS(approved=True))
    assert len(events)==1
    news=[s for s in rows(observer)["S"]["steps"] if s["stage"]=="news"]
    assert len(news)==1 and news[0]["status"]=="pass"
    assert observer.export()["complete"] is True


def test_cancellation_inside_emit_preserves_created_signal_and_unknown_later_candidates(monkeypatch):
    observer=observer_buffer()
    _,_,events,bot=run_cycle(monkeypatch,stocks=[stock("S"),stock("T")],observer=observer,
                            actual_trace=True,emit_error=asyncio.CancelledError())
    result=rows(observer)
    assert result["S"]["outcome"]=="signal_created"
    assert result["S"]["signal_id"]==events[0].id
    assert result["T"]["outcome"]=="unknown"
    assert result["T"]["terminal_reason"]=="scope_interrupted"
    assert not bot._daily_entry_count and not bot._screening_signal_cooldown


@pytest.mark.parametrize("hour,reasons,ratio,threshold", [
    (10,["MA20+3%","기관 순매수"],1.5,1.5),
    (11,["MA20+3%"],2.5,2.5),
])
def test_momentum_effective_volume_and_early_supply_exemption(monkeypatch,hour,reasons,ratio,threshold):
    trace,_,events,_=run_cycle(monkeypatch,enabled=["momentum_breakout"],hour=hour,
                              stocks=[stock(score=80,reasons=reasons,volume_ratio=ratio)])
    assert len(events)==1
    assert checks(trace)["momentum_volume"][1]["threshold"]==threshold
    assert ("note",("early_supply","not_applicable"),{
        "symbol":"S","reason":"time_or_supply_exempt"}) in trace.calls


@pytest.mark.parametrize("regime", [{},{"change_pct":float("nan")},{"change_pct":False}])
def test_missing_or_invalid_regime_input_is_unknown_without_changing_fallback(monkeypatch,regime):
    observer=observer_buffer()
    _,_,events,_=run_cycle(monkeypatch,regime=regime,observer=observer,actual_trace=True)
    assert len(events)==1
    step=next(s for s in rows(observer)["S"]["steps"] if s["stage"]=="regime")
    assert step["status"]=="unknown"


def test_ma20_pass_below_three_percent_names_the_decisive_evidence(monkeypatch):
    trace,_,events,_=run_cycle(monkeypatch,enabled=["momentum_breakout"],stocks=[stock(reasons=["MA20+3%"],score=90)])
    assert len(events)==1
    assert checks(trace)["momentum_strength"][1]["reason"]=="ma20_evidence"


@pytest.mark.parametrize("open_price", [0,-1,float("nan")])
def test_unavailable_open_skips_price_comparison_and_records_exemption(monkeypatch,open_price):
    observer=observer_buffer()
    _,_,events,_=run_cycle(monkeypatch,quote={"price":10000,"change_pct":2,"volume":100,"open":open_price},
                           observer=observer,actual_trace=True)
    assert len(events)==1
    step=next(s for s in rows(observer)["S"]["steps"] if s["stage"]=="open_price")
    assert (step["status"],step["reason"])==("not_applicable","open_unavailable")


def test_single_index_floor_keeps_all_operands_despite_weighted_average_passing(monkeypatch):
    observer=observer_buffer()
    _,_,events,_=run_cycle(monkeypatch,regime=({"change_pct":-3},{"change_pct":4}),observer=observer,actual_trace=True)
    assert not events
    step=rows(observer)["S"]["steps"][-1]
    assert step["stage"]=="regime" and step["status"]=="fail"
    assert "kospi=-3;kosdaq=4;weighted=" in step["value"]
    assert "either_index<=-2.5" in step["threshold"]


def test_partial_quote_does_not_erase_an_actually_failed_index_floor(monkeypatch):
    observer=observer_buffer()
    _,_,events,_=run_cycle(monkeypatch,regime=({}, {"change_pct":-3}),observer=observer,actual_trace=True)
    assert not events
    step=rows(observer)["S"]["steps"][-1]
    assert (step["stage"],step["status"],step["reason"]) == ("regime","fail","partial_quote_index_floor")
