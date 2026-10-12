"""Raw market-state preservation; synthetic frames, no network or trading changes."""
import pytest

from src.analytics.entry_observation import EntryObservationBuffer, capture_quote
from src.analytics.entry_observation_journal import ObservationJournal, read_observation_journal
from src.data.feeds.kis_websocket import KISWebSocketFeed

SOURCE_SHA = 'a22543dd4a1c64c01210eef281203771da0d1d58e2e7d4c08643c35a3fc60ff3'


async def quote(tr='H0STASP0', length=63, code='2'):
    feed = object.__new__(KISWebSocketFeed)
    feed._message_count = 0
    feed._quote_callbacks = []
    rows = []
    async def collect(event):
        rows.append(event)
    feed.on_quote(collect)
    fields = ['0'] * length
    for i, value in {0:'005930', 1:'100001', 2:'0', 3:'10100', 13:'10000', 23:'120', 33:'230'}.items():
        fields[i] = value
    if length >= 63:
        fields[62] = code
    await feed._handle_message(f'0|{tr}|001|' + '^'.join(fields))
    assert len(rows) == 1
    return rows[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['1', '2', '3', '5'])
async def test_exact_krx_schema_retains_market_code_without_certifying_continuous(code):
    event = await quote(code=code)
    assert (event.ask_price, event.bid_price, event.ask_size, event.bid_size) == (10100, 10000, 120, 230)
    p = event.metadata
    assert p['market_class_code'] == code
    assert p['raw_field_count'] == 63
    assert p['market_state_schema'] == 'H0STASP0-63-v1'
    assert p['market_state_source_sha256'] == SOURCE_SHA
    assert p['market_state_status'] == 'raw_observed'
    assert p['source_as_of'] is None
    assert not any(k in p for k in ('market_session', 'vi_inactive', 'continuous_trading'))


@pytest.mark.asyncio
@pytest.mark.parametrize('tr,length', [('H0STASP0',59), ('H0STASP0',62), ('H0STASP0',64),
                                      ('H0NXASP0',63), ('H0NXASP0',65)])
async def test_other_shapes_keep_quotes_but_never_apply_krx_tail_offset(tr,length):
    p = (await quote(tr=tr, length=length)).metadata
    assert p['raw_field_count'] == length
    assert p['market_state_status'] == 'unsupported_schema'
    assert p['market_class_code'] is None
    assert p['market_state_schema'] is None
    assert p['market_state_source_sha256'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('code', ['', '0', '7', '22'])
async def test_unrecognized_code_is_preserved_as_invalid_raw_evidence(code):
    p = (await quote(code=code)).metadata
    assert p['market_class_code'] == code
    assert p['market_state_status'] == 'invalid_code'
    assert p['source_as_of'] is None


@pytest.mark.asyncio
async def test_state_provenance_survives_capture_journal_seal_and_reload(tmp_path):
    event = await quote()
    buffer = EntryObservationBuffer(evaluation_epoch='synthetic', capacity=10)
    path = tmp_path / 'capture.jsonl'
    journal = await ObservationJournal.open(buffer, path, study_ref='synthetic', study_sha256='a'*64,
        queue_capacity=10, batch_size=2, max_bytes=100000, max_record_bytes=10000)
    try:
        capture_quote(buffer, event)
    finally:
        sealed = await journal.close()
    assert sealed['sealed']
    result = read_observation_journal(path, max_bytes=100000, expected_study_sha256='a'*64)
    assert result['complete']
    p = result['records'][0]['provenance']
    assert p['market_class_code'] == '2'
    assert p['market_state_source_sha256'] == SOURCE_SHA
    assert p['raw_field_count'] == 63
    assert p['source_as_of'] is None
    assert p['market_state_status'] == 'raw_observed'
