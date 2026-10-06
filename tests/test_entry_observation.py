"""실자료 접점의 잔량·시각·분모·관측 격리 계약 — 합성 입력 전용."""
from __future__ import annotations

import asyncio
import ast
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)) if str(ROOT) not in sys.path else None


def observation():
    try:
        return importlib.import_module("src.analytics.entry_observation")
    except ModuleNotFoundError:
        pytest.fail("후보·호가 관측 연결 모듈 미구현")


@pytest.fixture(autouse=True)
def fixed_observation_clock(monkeypatch):
    monkeypatch.setattr(observation(), "_now", lambda: "2026-10-01T00:30:00+00:00")


def frame(tr="H0STASP0", count=1, hour="100001"):
    fields = ["0"] * 59
    for i, value in {0:"005930", 1:hour, 2:"0", 3:"10100", 4:"10200", 13:"10000",
                     14:"9900", 23:"120", 33:"230"}.items():
        fields[i] = value
    return f"0|{tr}|{count:03}|" + "^".join(fields)


def feed():
    from src.data.feeds.kis_websocket import KISWebSocketFeed
    # 네트워크/토큰을 생성하는 __init__ 없이 실제 dispatch/파서만 실행한다.
    obj = object.__new__(KISWebSocketFeed)
    obj._message_count = 0
    obj._quote_callbacks = []
    return obj


@pytest.mark.parametrize("tr", ["H0STASP0", "H0NXASP0"])
def test_native_quote_uses_quantity_columns_and_preserves_provenance(tr):
    f = feed(); events = []
    async def collect(event): events.append(event)
    f.on_quote(collect)
    asyncio.run(f._handle_message(frame(tr)))
    e = events[0]
    assert (e.ask_price, e.bid_price, e.ask_size, e.bid_size) == (10100, 10000, 120, 230)
    assert e.metadata["tr_id"] == tr
    assert e.metadata["exchange_time"] == "100001"
    assert e.metadata["message_count"] == 1
    assert datetime.fromisoformat(e.metadata["received_at"]).utcoffset() is not None
    assert e.metadata["source_as_of"] is None  # 날짜 없는 시각을 검증된 날짜로 바꾸지 않는다.
    assert e.timestamp.tzinfo is None  # 기존 이벤트 정렬의 시각 의미는 보존한다.


@pytest.mark.parametrize("hour,count", [("bad",1),("",1)])
def test_invalid_clock_is_not_promoted(hour,count):
    f = feed(); events=[]
    async def collect(event): events.append(event)
    f.on_quote(collect)
    asyncio.run(f._handle_message(frame(count=count,hour=hour)))
    assert len(events) == 1
    assert events[0].metadata["message_count"] == count
    assert events[0].metadata["exchange_time"] == hour
    assert events[0].metadata["source_as_of"] is None


def test_multi_record_book_frame_is_rejected_without_partial_callback():
    f = feed(); events = []
    async def collect(event): events.append(event)
    f.on_quote(collect)
    asyncio.run(f._handle_message(frame(count=2)))
    assert events == []


def test_buffer_copies_inputs_and_exposes_overflow():
    m=observation(); b=m.EntryObservationBuffer(evaluation_epoch="fixed-v1", capacity=2)
    one={"kind":"scan","candidates":[{"symbol":"005930"}]}
    b.publish(one); one["candidates"][0]["symbol"]="CHANGED"
    b.publish({"kind":"second"}); b.publish({"kind":"lost"})
    exported=b.export()
    assert exported["records"][0]["candidates"][0]["symbol"]=="005930"
    assert exported["dropped_records"]==1
    assert exported["complete"] is False
    exported["records"].clear()
    assert len(b.export()["records"])==2


def test_scan_and_rest_preserve_full_returned_population_without_inventing_time():
    m=observation(); b=m.EntryObservationBuffer(evaluation_epoch="fixed-v1", capacity=10)
    stocks=[SimpleNamespace(symbol=f"S{i}",score=i,price=10000,screened_at=datetime(2026,10,1,10)) for i in range(25)]
    sid=m.capture_scan(b,stocks,"regular")
    quote={"high":11000,"price":10000,"app_secret":"DO_NOT_COPY"}
    before=deepcopy(quote)
    m.capture_rest_quote(b,sid,"S0",quote)
    records=b.export()["records"]
    assert len(records[0]["candidates"])==25
    assert len({c["candidate_id"] for c in records[0]["candidates"]})==25
    r=records[1]
    assert r["candidate_id"]==records[0]["candidates"][0]["candidate_id"]
    assert r["quote"]["high"]==11000 and r["source_as_of"] is None
    assert "app_secret" not in r["quote"] and quote==before


def test_hooks_are_optional_and_observer_failures_do_not_escape():
    m=observation()
    class Broken:
        def publish(self, record):
            record.clear()
            raise RuntimeError("synthetic observer failure")
    stock=SimpleNamespace(symbol="S",score=90,price=10000)
    assert m.capture_scan(None,[stock],"regular") is None
    assert m.capture_scan(Broken(),[stock],"regular") is None
    assert stock.symbol=="S" and stock.score==90
    assert m.capture_rest_quote(Broken(),"scan","S",{"high":11000}) is None


def test_signal_capture_has_real_event_id_but_no_baseline_approval():
    from src.core.event import SignalEvent
    from src.core.types import StrategyType
    m=observation(); b=m.EntryObservationBuffer(evaluation_epoch="fixed-v1", capacity=10)
    event=SignalEvent(symbol="S",strategy=StrategyType.GAP_AND_GO,price=Decimal("10000"))
    m.capture_signal(b,"scan",event)
    m.capture_emit_result(b,"scan","S",event.id,False)
    rows=b.export()["records"]
    assert rows[0]["signal_id"]==rows[1]["signal_id"]==event.id
    assert rows[0]["candidate_id"]==rows[1]["candidate_id"]=="scan:S"
    assert rows[1]["emitted"] is False
    assert "baseline_eligible" not in rows[0]


def context():
    return {"schema_version":1,"dataset_kind":"synthetic","evaluation_epoch":"fixed-v1",
            "as_of":"2026-10-01T16:00:00+09:00","fee_model_ref":"synthetic-zero",
            "fees":{"buy_commission_rate":"0","sell_commission_rate":"0","sell_tax_rate":"0"},
            "policy":{"max_quote_age_seconds":5,"max_decision_delay_seconds":5,
                      "entry_slippage_bps":"0","exit_slippage_bps":"0"}}


def test_assembly_retains_unselected_candidates_and_does_not_infer_baseline():
    m=observation(); b=m.EntryObservationBuffer(evaluation_epoch="fixed-v1", capacity=10)
    sid=m.capture_scan(b,[SimpleNamespace(symbol="A"),SimpleNamespace(symbol="B")],"regular")
    m.capture_rest_quote(b,sid,"A",{"high":11000,"price":10000})
    prepared=m.prepare_input(context(),b.export(),[])
    assert len(prepared["payload"]["opportunities"])==2
    assert prepared["ready_opportunities"]==0
    assert prepared["population_scope"]=="returned_screen_candidates"
    from src.analytics.entry_price_shadow import build_report
    r=build_report(prepared["payload"])
    assert r["counts"]["unknown"]==2
    assert r["summary"]["complete_delta_net_pnl"] is None


def test_assembly_rejects_orphan_duplicate_epoch_and_incomplete_capture():
    m=observation(); b=m.EntryObservationBuffer(evaluation_epoch="fixed-v1", capacity=1)
    sid=m.capture_scan(b,[SimpleNamespace(symbol="A")],"regular")
    for inputs in ([{"opportunity_id":"orphan"}],
                   [{"opportunity_id":f"{sid}:A","evaluation_epoch":"wrong"}],
                   [{"opportunity_id":f"{sid}:A"}]*2):
        with pytest.raises(ValueError): m.prepare_input(context(),b.export(),inputs)
    b.publish({"kind":"overflow"})
    prepared=m.prepare_input(context(),b.export(),[])
    assert prepared["capture_complete"] is False
    assert prepared["ready_opportunities"]==0


@pytest.mark.parametrize("fail", [False, True])
def test_observed_emit_preserves_event_and_original_success_or_failure(fail):
    from src.core.event import SignalEvent
    m=observation(); b=m.EntryObservationBuffer(evaluation_epoch="fixed-v1",capacity=10)
    event=SignalEvent(symbol="S"); original=deepcopy(event)
    class Engine:
        async def emit(self, received):
            assert received is event
            if fail: raise RuntimeError("original emit failure")
            return "queued"
    if fail:
        with pytest.raises(RuntimeError,match="original emit failure"):
            asyncio.run(m.emit_with_observation(Engine(),event,b,"scan"))
    else:
        assert asyncio.run(m.emit_with_observation(Engine(),event,b,"scan"))=="queued"
    assert event==original
    assert b.export()["records"][-1]["emitted"] is (not fail)


@pytest.mark.parametrize("subscription_failure", [False, True])
@pytest.mark.parametrize("selection_enabled", [None, 'selection-basis-v1', 'selection-basis-v2'])
def test_real_screen_loop_records_before_strategy_gate(monkeypatch, subscription_failure, selection_enabled):
    """운영 초기화 없이 실제 루프를 한 번 실행, 비활성 전략 후보도 분모에 남는다."""
    from loguru import logger
    from src.core.types import MarketSession
    m=observation()
    settings = {'version':selection_enabled,'max_candidates':100,'max_source_terms':16} if selection_enabled else None
    b=m.EntryObservationBuffer(evaluation_epoch="fixed-v1",capacity=10,selection_basis_settings=settings)
    stocks=[SimpleNamespace(symbol="S",name="합성",price=10000,score=80,change_pct=1,reasons=[])]
    screening_args=[]
    async def screen_all(**kwargs):
        screening_args.append(kwargs)
        return stocks
    bot=SimpleNamespace(running=True,screener=SimpleNamespace(screen_all=screen_all),theme_detector=None,
                        _watch_symbols_lock=asyncio.Lock(),_watch_symbols=[],_screening_interval=60,
                        strategy_manager=SimpleNamespace(enabled_strategies=[]),
                        config=SimpleNamespace(get=lambda *a:{}),_entry_price_observer=b)
    if subscription_failure:
        def fail_submit(*args): raise RuntimeError("synthetic enrollment failure")
        bot.ws_feed=SimpleNamespace(_quote_subscription_owner=SimpleNamespace(observer=b,submit=fail_submit))
    calls=[]
    async def sleep(delay):
        calls.append(delay)
        if len(calls)>1: bot.running=False
    scope={"asyncio":SimpleNamespace(sleep=sleep),"datetime":datetime,"logger":logger,
           "MarketSession":MarketSession,"capture_scan":m.capture_scan,
           "begin_gate_trace":__import__("src.analytics.entry_gate_trace", fromlist=["begin_gate_trace"]).begin_gate_trace,
           "safe_trace_call":__import__("src.analytics.entry_gate_trace", fromlist=["safe_trace_call"]).safe_trace_call,
           "observe_screen_candidates":__import__("src.data.feeds.quote_subscription", fromlist=["observe_screen_candidates"]).observe_screen_candidates,
           "_hb":SimpleNamespace(**{k:lambda *a,**kw:None for k in ("record_attempt","record_idle","record_success","record_failure")}),
           "trading_logger":SimpleNamespace(log_screening=lambda **kwargs:None)}
    tree=ast.parse((ROOT/'src/schedulers/kr_scheduler.py').read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='KRScheduler')
    method=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='run_screening')
    exec(compile(ast.Module(body=[method],type_ignores=[]),'real_screening_loop','exec'),scope)
    obj=SimpleNamespace(bot=bot,_get_current_session=lambda:MarketSession.REGULAR)
    asyncio.run(scope['run_screening'](obj))
    assert bot._last_screened is stocks
    assert (screening_args[0].get("capture_selection") is True) is bool(selection_enabled)
    assert (screening_args[0].get("capture_selection_sources") is True) is (selection_enabled == 'selection-basis-v2')
    assert bot._watch_symbols == ["S"]
    assert b.export()["complete"] is (not subscription_failure)
    scan=b.export()["records"][0]
    assert scan["candidates"][0]["symbol"]=="S"
    assert scan["population_scope"]=="returned_screen_candidates"


def test_native_quote_callback_reaches_observation_buffer():
    m=observation(); b=m.EntryObservationBuffer(evaluation_epoch="fixed-v1",capacity=10)
    tree=ast.parse((ROOT/'scripts/run_trader.py').read_text())
    methods=[n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='_on_entry_price_quote']
    assert len(methods)==1, '실제 Runner 호가 관측 콜백 미구현'
    scope={"capture_quote":m.capture_quote}
    exec(compile(ast.Module(body=methods,type_ignores=[]),'real_quote_callback','exec'),scope)
    obj=SimpleNamespace(_entry_price_observer=b)
    f=feed()
    async def forward(e): await scope['_on_entry_price_quote'](obj,e)
    f.on_quote(forward)
    asyncio.run(f._handle_message(frame()))
    saved=b.export()['records'][0]
    assert saved['ask_size']==120 and saved['bid_size']==230
    assert saved['provenance']['tr_id']=='H0STASP0'


def test_cli_observation_adapter_keeps_unknown_rows(tmp_path,capsys):
    m=observation(); b=m.EntryObservationBuffer(evaluation_epoch="fixed-v1",capacity=10)
    m.capture_scan(b,[SimpleNamespace(symbol="S")],"regular")
    p=tmp_path/'observations.json'
    import json
    p.write_text(json.dumps({'context':context(),'observations':b.export(),'evaluation_inputs':[]}))
    from scripts.compare_entry_price_shadow import main
    assert main(['--observations',str(p)])==0
    result=json.loads(capsys.readouterr().out)
    assert result['report']['counts']['unknown']==1
    assert result['ready_opportunities']==0


def test_explicit_inputs_reach_calculator_but_capture_loss_prevents_complete_result():
    m=observation(); b=m.EntryObservationBuffer(evaluation_epoch="fixed-v1",capacity=1)
    sid=m.capture_scan(b,[SimpleNamespace(symbol="S")],"regular")
    t="2026-10-01T10:00:00+09:00"
    supplied={"opportunity_id":f"{sid}:S", "strategy":"gap_and_go", "decision_at":t,
              "baseline_basis":"order_free_policy","baseline_eligible":True,"quantity":100,"capital_budget":"1100000",
              "target":{"price":"11000","basis":"intraday_high_at_decision","as_of":t,"available_at":t},
              "stop":{"pct":"5","basis":"net_pnl","policy_ref":"same-exit","as_of":t,"available_at":t},
              "quote":{"ask":"10000","bid":"9990","ask_size":100,"session":"KRX_REGULAR_CONTINUOUS",
                       "as_of":"2026-10-01T10:00:01+09:00","received_at":"2026-10-01T10:00:02+09:00"}}
    p=m.prepare_input(context(),b.export(),[supplied])
    assert p['ready_opportunities']==1
    assert p['report']['opportunities'][0]['max_ask_whole_krw']=='10232'
    assert p['report']['opportunities'][0]['a_net_pnl'] is None
    b.publish({'kind':'overflow'})
    p=m.prepare_input(context(),b.export(),[supplied])
    assert p['ready_opportunities']==0 and p['report']['counts']['unknown']==1
    assert p['report']['summary']['complete_delta_net_pnl'] is None


def test_observation_exception_is_preserved_as_capture_loss():
    m=observation()
    class FailingBuffer(m.EntryObservationBuffer):
        def publish(self, record): raise ValueError('synthetic failure')
    b=FailingBuffer(evaluation_epoch='fixed-v1',capacity=2)
    assert m.capture_scan(b,[SimpleNamespace(symbol='S')],'regular') is None
    assert b.export()['complete'] is False
    assert b.export()['dropped_records']==1


def test_observation_build_error_does_not_hide_missing_scan():
    m=observation(); b=m.EntryObservationBuffer(evaluation_epoch='fixed-v1',capacity=2)
    assert m.capture_scan(b,[object()],'regular') is None
    assert b.export()['complete'] is False


@pytest.mark.parametrize('observed_at,decision_at',[
    ('2026-10-01T11:00:00+09:00','2026-10-01T10:00:00+09:00'),
    ('2026-10-01T17:00:00+09:00',None),
    ('2026-10-01T09:00:00',None),
])
def test_assembly_never_moves_candidates_before_their_observation(observed_at,decision_at):
    m=observation(); b=m.EntryObservationBuffer(evaluation_epoch='fixed-v1',capacity=2)
    sid=m.capture_scan(b,[SimpleNamespace(symbol='S')],'regular')
    saved=b.export(); saved['records'][0]['observed_at']=observed_at
    inputs=[] if decision_at is None else [
        {'opportunity_id':f'{sid}:S','decision_at':decision_at,'strategy':'gap_and_go',
         'baseline_basis':'order_free_policy','baseline_eligible':False}]
    with pytest.raises(ValueError,match='시각'):
        m.prepare_input(context(),saved,inputs)
