"""기존 단일 fake socket에서 정적 기술 구간과 운영 시세를 함께 검증한다."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src.analytics.entry_observation import capture_quote, capture_scan
from src.analytics.entry_observation_journal import read_observation_journal
from src.analytics.entry_observation_runtime import ObservationRuntime
from src.dashboard.kr_api import KRAPIHandler
from test_entry_observation_readiness import request
from test_market_state_pilot_contract import NOW, plan
from test_market_state_capture import frame, PRICE, BOOK, STATUS
from test_quote_subscription import make_feed, ack


def bot_with(holdings):
    feed=make_feed(); feed._priority_symbols=set(holdings)
    return SimpleNamespace(market='kr', dry_run=False, ws_feed=feed)


def journal(p):
    return read_observation_journal(p.settings['journal_path'],max_bytes=p.settings['max_bytes'],
                                    expected_study_sha256=p.study_sha256)


@pytest.mark.asyncio
async def test_static_pilot_single_socket_no_economic_hooks_and_clean_seal(tmp_path,monkeypatch):
    p=plan(tmp_path); b=bot_with(['000660']); clock=[NOW]
    import src.data.feeds.kis_websocket as ws_module
    monkeypatch.setattr(ws_module,'datetime',SimpleNamespace(now=lambda tz=None:clock[0]))
    r=await ObservationRuntime.install(b,p,now=lambda:clock[0])
    q=b.ws_feed._quote_subscription_owner; q.interval=0
    assert q.pilot_snapshot()['installed'] and not q.pilot_snapshot()['stream_ready']
    assert q.snapshot()['operational_demand']==2
    assert capture_scan(r.buffer,[], 'regular') is None
    seen=[]
    async def price(e): seen.append(e.symbol)
    async def book(e): capture_quote(r.buffer,e)
    b.ws_feed.on_market_data(price); b.ws_feed.on_quote(book)
    try:
        assert await b.ws_feed.connect()
        keys=[(x['body']['input']['tr_id'],x['body']['input']['tr_key']) for x in b.ws_feed._ws.sent]
        assert keys[:2]==[(PRICE,'000660'),(BOOK,'000660')]
        assert set(keys[2:])=={(tr,'005490') for tr in (PRICE,BOOK,STATUS)}
        for key in keys: await q.handle_ack(ack(key),q.generation)
        assert q.pilot_snapshot()['stream_ready']
        clock[0]=p.start_at
        for tr in (BOOK,PRICE,STATUS):
            await b.ws_feed._handle_message(frame(tr,'005490'),socket=b.ws_feed._ws,generation=q.generation)
        await b.ws_feed._handle_message(frame(PRICE,'000660'),socket=b.ws_feed._ws,generation=q.generation)
        assert seen==['000660'] and q.operational_coverage()=={'000660'}
        readiness=await KRAPIHandler(SimpleNamespace(bot=b)).get_entry_observation_readiness(request())
        data=json.loads(readiness.text)
        assert data['scan_scope']=='pilot' and data['pilot']['stream_ready']
        assert data['pilot']['observed_channels']==3
        clock[0]=p.end_at; r.start(); await asyncio.wait_for(r.task,2)
        assert r.result['sealed'] and journal(p)['complete']
        assert q.snapshot()['leases']==0 and b.ws_feed.is_connected
        assert not any(x['kind'] in ('scan','signal','order_ready') for x in journal(p)['records'])
    finally:
        await r.close('test_end'); await b.ws_feed.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('count,overlap,admitted',[(14,False,True),(15,False,False),(15,True,True),(16,True,False)])
async def test_exact_capacity_boundary_skips_only_observation(tmp_path,count,overlap,admitted):
    p=plan(tmp_path); holdings=[f'{100000+i}' for i in range(count)]
    if overlap: holdings[0]='005490'
    b=bot_with(holdings)
    original=set(b.ws_feed._subscribed_symbols)
    r=await ObservationRuntime.install(b,p,now=lambda:NOW)
    if admitted:
        assert r is not None and b.ws_feed._quote_subscription_owner.pilot_snapshot()['installed']
        await r.close('test_end')
    else:
        assert r is None and getattr(b.ws_feed,'_quote_subscription_owner',None) is None
        assert getattr(b,'_entry_price_observer',None) is None
        assert b.ws_feed._priority_symbols==set(holdings) and b.ws_feed._subscribed_symbols==original
        assert b._entry_observation_close_result['sealed']
        assert 'pilot_channel_capacity' in journal(p)['incomplete_reasons']


@pytest.mark.asyncio
async def test_demand_increase_ends_only_pilot_and_exposes_insufficient_capacity(tmp_path):
    p=plan(tmp_path); b=bot_with(['000660']); r=await ObservationRuntime.install(b,p,now=lambda:NOW)
    q=b.ws_feed._quote_subscription_owner; q.interval=0
    try:
        assert await b.ws_feed.connect()
        for key in list(q._states): await q.handle_ack(ack(key),q.generation)
        await q.set_operational([str(100000+i) for i in range(15)],PRICE,BOOK)
        assert r.buffer.ended and q.snapshot()['leases']==0
        assert not q.pilot_snapshot()['installed']
        assert b.ws_feed.is_connected and q.snapshot()['operational_demand']==30
        assert 'pilot_channel_capacity' in r.buffer.capture_status()['incomplete_reasons']
        await r.close()
        assert not journal(p)['complete']
    finally:
        await r.close('test_end'); await b.ws_feed.disconnect()


@pytest.mark.asyncio
async def test_static_lease_deadline_and_duplicate_cannot_extend(tmp_path):
    p=plan(tmp_path); b=bot_with([]); r=await ObservationRuntime.install(b,p,now=lambda:NOW)
    q=b.ws_feed._quote_subscription_owner
    try:
        lease=next(iter(q._leases.values()))
        assert lease.expires_at==p.end_at.isoformat()
        assert 359 < lease.deadline-q.clock() <= 360
        with pytest.raises(RuntimeError): q.install_static_lease('005930',expires_at=p.end_at)
        assert list(q._leases.values())==[lease]
    finally: await r.close('test_end')


@pytest.mark.asyncio
async def test_runner_capacity_skip_does_not_start_or_shutdown_runtime(monkeypatch):
    from scripts.run_trader import UnifiedTradingBot
    called=[]
    async def skipped(*args,**kwargs): called.append('skip'); return None
    monkeypatch.setattr(ObservationRuntime,'install',skipped)
    b=SimpleNamespace(_entry_observation_plan=object())
    await UnifiedTradingBot._install_entry_observation(b)
    assert called==['skip']

@pytest.mark.asyncio
async def test_unknown_or_rejected_channel_is_not_stream_ready(tmp_path):
    p=plan(tmp_path); b=bot_with(['000660']); r=await ObservationRuntime.install(b,p,now=lambda:NOW)
    q=b.ws_feed._quote_subscription_owner; q.interval=0
    try:
        assert await b.ws_feed.connect()
        for key in list(q._states): await q.handle_ack(ack(key),q.generation)
        assert q.pilot_snapshot()['stream_ready']
        q._states[(STATUS,'005490')].status='unknown'
        assert not q.pilot_snapshot()['stream_ready']
        assert q.pilot_snapshot()['occupied']==5
    finally:
        await r.close('test_end'); await b.ws_feed.disconnect()


@pytest.mark.asyncio
async def test_preparation_lease_survives_until_window_then_reconnect_and_clock_end(tmp_path):
    p=plan(tmp_path); b=bot_with([]); clock=[NOW]
    r=await ObservationRuntime.install(b,p,now=lambda:clock[0])
    q=b.ws_feed._quote_subscription_owner; q.interval=0
    mono=[q.clock()]; q.clock=lambda:mono[0]
    try:
        mono[0]+=60; clock[0]=p.start_at
        assert await b.ws_feed.connect()
        assert q.pilot_snapshot()['installed']
        await b.ws_feed._close_managed_socket(b.ws_feed._ws,q.generation)
        # reconnect may replace only the socket generation, never the lease deadline.
        deadline=next(iter(q._leases.values())).deadline
        assert await b.ws_feed.connect()
        assert next(iter(q._leases.values())).deadline==deadline and q.generation==2
        clock[0]=p.end_at
        assert not q.accepts_market_state((PRICE,'005490'),q.generation)
        assert q.snapshot()['leases']==0
        await r.close()
        assert not journal(p)['complete']  # explicit gap remains despite reconnection.
    finally:
        await r.close('test_end'); await b.ws_feed.disconnect()

@pytest.mark.asyncio
async def test_static_contract_cannot_enroll_any_economic_identity(tmp_path):
    p=plan(tmp_path); b=bot_with([]); r=await ObservationRuntime.install(b,p,now=lambda:NOW)
    q=b.ws_feed._quote_subscription_owner
    try:
        expected=dict(q._leases)
        q.submit_order_ready(dict(signal_id='unexpected',symbol='005930'))
        await q.enroll('unexpected',['005930'])
        await q.cancel_pending()
        assert q._ready_requests=={} and q._leases==expected
    finally: await r.close('test_end')

@pytest.mark.asyncio
async def test_real_constructor_closed_session_initialization_needs_no_api_or_socket(tmp_path,monkeypatch):
    import src.data.feeds.kis_websocket as ws_module
    from src.core.types import MarketSession
    monkeypatch.setattr(ws_module,'get_token_manager',lambda:object())
    feed=ws_module.KISWebSocketFeed(ws_module.KISWebSocketConfig(ws_url='synthetic'))
    assert feed._current_session==MarketSession.CLOSED
    feed.set_priority_symbols(['000660'])
    b=SimpleNamespace(market='kr',dry_run=False,ws_feed=feed)
    r=await ObservationRuntime.install(b,plan(tmp_path),now=lambda:NOW)
    try:
        q=feed._quote_subscription_owner
        assert q._operational==[(PRICE,'000660'),(BOOK,'000660')]
        assert q.pilot_snapshot()['installed']
        assert not feed._connected and feed._session is None and feed._ws is None
    finally: await r.close('test_end')
