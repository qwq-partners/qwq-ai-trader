"""Single synthetic socket only: market-state evidence must never become a price signal."""
import asyncio
import json

import pytest

from src.analytics.entry_observation import EntryObservationBuffer, capture_quote
from src.data.feeds.quote_subscription import QuoteSubscriptionCoordinator
from test_quote_subscription import make_feed, ack

PRICE, BOOK, STATUS = 'H0STCNT0', 'H0STASP0', 'H0STMKO0'


def contract(**kw):
    return dict(registration_cap=41, external_reserved=0, operational_headroom=0,
                max_candidates=5, lease_seconds=600, evidence_ref='synthetic',
                request_interval=0, market_state_evidence=True, **kw)


def fields(tr, symbol='005930', width=None):
    row = ['0'] * {PRICE: 47, BOOK: 63, STATUS: 11}[tr]
    row[0] = symbol
    if tr == PRICE:
        row[1:6] = ['100001', '10000', '2', '10', '0.1']
        row[33:36] = ['20261013', '20', 'N']
        row[43], row[46] = '0', '2'
    elif tr == BOOK:
        row[1], row[3], row[13], row[23], row[33], row[62] = '100002', '10010', '10000', '50', '40', '2'
    else:
        row[1:11] = ['N', 'discard-free-text', '112', '', '0', '0', '00', 'N', 'N', '1']
    return row if width is None else row[:width]


def frame(tr, symbol='005930', width=None):
    return f'0|{tr}|001|' + '^'.join(fields(tr, symbol, width))


async def feed_setup(buffer=None, **changes):
    f = make_feed()
    b = buffer or EntryObservationBuffer(evaluation_epoch='synthetic', capacity=1000)
    settings = contract()
    settings.update(changes)
    f.enable_quote_observation(b, **settings)
    q = f._quote_subscription_owner
    await q.enroll('scan', ['005930'])
    assert await f.connect()
    return f, b, q


async def receive(f, q, data):
    await f._handle_message(data, socket=f._ws, generation=q.generation)


def states(b):
    return [r for r in b.export()['records'] if r['kind'] == 'market_state']


@pytest.mark.asyncio
@pytest.mark.parametrize('cap,headroom,operational,expected', [
    (2, 0, [], []), (3, 0, [], [BOOK, PRICE, STATUS]),
    (3, 1, [], []), (3, 0, ['005930'], [PRICE, BOOK, STATUS]),
    (4, 0, ['000660'], [PRICE, BOOK]),
])
async def test_bundle_admission_respects_whole_demand_and_operational_sharing(cap, headroom, operational, expected):
    b = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=100)
    settings = contract(); settings.update(registration_cap=cap, operational_headroom=headroom)
    q = QuoteSubscriptionCoordinator(b, **settings)
    sent = []
    async def send(action, key): sent.append((action, key))
    await q.set_operational(operational, PRICE, BOOK)
    await q.start(send)
    await q.enroll('scan', ['005930'])
    assert [key[0] for action, key in sent if action == 'subscribe'] == expected
    allocation = [r for r in b.export()['records'] if r.get('candidate_id') == 'scan:005930'][-1]
    assert (allocation['status'] != 'unallocated') == (STATUS in expected)
    assert q.snapshot()['occupied'] <= cap


@pytest.mark.asyncio
async def test_three_channels_share_connection_and_state_only_price_never_enters_engine():
    f, b, q = await feed_setup()
    seen = []
    async def price(e): seen.append(e)
    async def book(e): capture_quote(b, e)
    f.on_market_data(price); f.on_quote(book)
    try:
        assert {m['body']['input']['tr_id'] for m in f._ws.sent} == {PRICE, BOOK, STATUS}
        for tr in (BOOK, PRICE, STATUS):
            await receive(f, q, json.dumps(ack((tr, '005930'))))
        await receive(f, q, frame(PRICE))
        await receive(f, q, frame(STATUS))
        await receive(f, q, frame(BOOK))
        rows = states(b)
        quote = [r for r in b.export()['records'] if r['kind'] == 'ws_quote'][0]
        assert [r['tr_id'] for r in rows] == [PRICE, STATUS]
        assert all(r['state_verdict'] == 'UNKNOWN' and r['source_sequence'] is None for r in rows)
        assert rows[0]['business_date'] == '20261013'
        assert rows[1]['business_date'] is None and rows[1]['exchange_time'] is None
        assert rows[0]['local_frame_index'] < rows[1]['local_frame_index'] < quote['provenance']['local_frame_index']
        assert all(r['connection_id'] == quote['provenance']['connection_id'] and r['generation'] == q.generation for r in rows)
        assert seen == [] and q.operational_coverage() == set() and not f._subscribed_symbols
        assert q.snapshot()['observed'] == 3
        assert 'discard-free-text' not in json.dumps(b.export())
    finally:
        await f.disconnect()


@pytest.mark.asyncio
async def test_price_batch_preserves_receipt_order_and_only_operational_callback():
    f, b, q = await feed_setup()
    seen = []
    async def collect(e): seen.append(e.symbol)
    f.on_market_data(collect)
    try:
        await q.set_operational(['000660'], PRICE, BOOK)
        await q.enroll('scan2', ['000660'])
        batch = '^'.join(fields(PRICE) + fields(PRICE, '000660'))
        await receive(f, q, f'0|{PRICE}|002|' + batch)
        rows = states(b)
        assert [r['record_index'] for r in rows] == [1, 2]
        assert {r['message_count'] for r in rows} == {2}
        assert len({(r['local_frame_index'], r['received_at']) for r in rows}) == 1
        assert seen == ['000660']
        assert q.operational_coverage() == set()  # no ACK yet
    finally:
        await f.disconnect()


@pytest.mark.asyncio
async def test_legacy_46_is_unknown_evidence_without_breaking_operational_price():
    f, b, q = await feed_setup()
    seen = []
    async def collect(e): seen.append(e.symbol)
    f.on_market_data(collect)
    try:
        await q.set_operational(['005930'], PRICE, BOOK)
        await receive(f, q, frame(PRICE, width=46))
        row = states(b)[0]
        assert row['market_state_status'] == 'unsupported_layout'
        assert row['business_date'] is None and row['new_mkop_cls_code'] is None
        assert seen == ['005930'] and not b.export()['complete']
    finally:
        await f.disconnect()


@pytest.mark.asyncio
async def test_old_socket_ended_lease_and_unregistered_symbol_never_create_state():
    f, b, q = await feed_setup()
    try:
        old, gen = f._ws, q.generation
        await receive(f, q, frame(STATUS, '000660'))
        await f._close_managed_socket(old, gen)
        assert await f.connect()
        await f._handle_message(frame(STATUS), socket=old, generation=gen)
        assert states(b) == []
        q.end_observation()
        await receive(f, q, frame(PRICE))
        await receive(f, q, frame(STATUS))
        assert states(b) == []
    finally:
        await f.disconnect()


@pytest.mark.asyncio
async def test_failed_state_copy_does_not_block_operational_price(monkeypatch):
    f, b, q = await feed_setup()
    seen = []
    async def collect(e): seen.append(e.symbol)
    f.on_market_data(collect)
    original = b.publish
    def fail_state(record):
        if record['kind'] == 'market_state': raise RuntimeError('synthetic')
        return original(record)
    try:
        await q.set_operational(['005930'], PRICE, BOOK)
        monkeypatch.setattr(b, 'publish', fail_state)
        await receive(f, q, frame(PRICE))
        assert seen == ['005930'] and not b.export()['complete']
    finally:
        await f.disconnect()


@pytest.mark.asyncio
async def test_state_receipt_cannot_establish_operational_price_coverage():
    f, b, q = await feed_setup()
    try:
        for tr in (BOOK, PRICE, STATUS):
            await receive(f, q, json.dumps(ack((tr, '005930'))))
        invalid = fields(PRICE); invalid[2] = 'bad-price'
        await receive(f, q, f'0|{PRICE}|001|' + '^'.join(invalid))
        assert len(states(b)) == 1
        await q.set_operational(['005930'], PRICE, BOOK)
        assert q.operational_coverage() == set()
        await receive(f, q, f'0|{PRICE}|001|' + '^'.join(invalid))
        assert q.operational_coverage() == set()
        await receive(f, q, frame(PRICE))
        assert q.operational_coverage() == {'005930'}
    finally:
        await f.disconnect()


@pytest.mark.asyncio
async def test_state_and_quote_provenance_survive_sealed_journal(tmp_path):
    from test_entry_observation_journal import opened, read
    b, journal, path = await opened(tmp_path, queue_capacity=100)
    f, _, q = await feed_setup(b)
    async def book(e): capture_quote(b, e)
    f.on_quote(book)
    try:
        await receive(f, q, frame(PRICE))
        await receive(f, q, frame(STATUS))
        await receive(f, q, frame(BOOK))
        q.end_observation()
        result = await journal.close()
        assert result['sealed'] and result['fsync_confirmed']
        loaded = read(path)
        assert loaded['complete'] and loaded['records'] == b.export()['records']
        assert len([r for r in loaded['records'] if r['kind'] == 'market_state']) == 2
    finally:
        await journal.close()
        await f.disconnect()


@pytest.mark.parametrize('flag', [True, False, 1, 'true', None])
def test_explicit_runtime_contract_boolean_only(tmp_path, flag):
    from test_entry_observation_runtime import load
    def change(c): c['capture']['channels']['market_state_evidence'] = flag
    if type(flag) is bool:
        plan, _ = load(tmp_path, change)
        assert plan.settings['channels']['market_state_evidence'] is flag
    else:
        with pytest.raises(ValueError): load(tmp_path, change)


def test_window_does_not_collect_state_before_start_or_at_end(tmp_path):
    from test_entry_observation_runtime import load, NOW
    from test_signal_window_observation import window_plan
    from src.analytics.entry_observation_runtime import _WindowBuffer
    plan, _ = load(tmp_path, window_plan)
    now = [NOW]
    b = _WindowBuffer(plan, lambda: now[0])
    record = dict(kind='market_state', observed_at=NOW.isoformat())
    assert b.publish(record) is False
    now[0] = plan.start_at
    assert b.publish(record) is True
    now[0] = plan.end_at
    assert b.publish(record) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('data', [frame(STATUS, width=10), frame(STATUS) + '^extra'])
async def test_unsupported_status_shape_is_unknown_and_incomplete(data):
    f, b, q = await feed_setup()
    try:
        await receive(f, q, data)
        row = states(b)[0]
        assert row['market_state_status'] == 'unsupported_layout'
        assert row['vi_cls_code'] is None and row['state_verdict'] == 'UNKNOWN'
        assert not b.export()['complete']
    finally:
        await f.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize('data', [frame(STATUS).replace('|001|', '|002|'),
                                  frame(STATUS).replace('0|', '1|', 1)])
async def test_unqualified_status_frame_is_not_partially_recorded(data):
    f, b, q = await feed_setup()
    try:
        await receive(f, q, data)
        assert states(b) == [] and not b.export()['complete']
    finally:
        await f.disconnect()


def test_offline_window_rejects_state_without_explicit_contract(tmp_path):
    from src.analytics.entry_observation_runtime import validate_window_contract
    from test_entry_observation_runtime import load
    from test_signal_window_observation import window_plan
    _, context = load(tmp_path, window_plan)
    with pytest.raises(ValueError, match='market.state'):
        validate_window_contract(context, {'records': [dict(kind='market_state',
            observed_at=context['capture']['start_at'])]})
