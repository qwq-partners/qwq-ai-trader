"""Offline transport, durable media, and scheduler/archive contract."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
from io import BytesIO
import json
import sqlite3
import sys
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from PIL import Image
import pytest

from src.analytics.audience_reports import MarketSnapshot
from src.analytics.daily_report import DailyReportGenerator, record_morning_brief_dispatch
from src.analytics.report_delivery import ReportStore
from src.schedulers.report_scheduler import ReportSchedule
from src.utils.telegram import TelegramNotifier

DAY = '2026-10-27'
NOW = datetime(2026, 10, 27, 7, 40, tzinfo=ZoneInfo('Asia/Seoul'))
MARKET = MarketSnapshot(sectors=({'name': '반도체', 'r1': 1.5, 'as_of': '2026-10-26', 'source': 'etf'},), kr_basis_date='2026-10-26')


@pytest.fixture
def png():
    out = BytesIO()
    Image.new('RGB', (720, 1000), 'white').save(out, format='PNG')
    return out.getvalue()


def notifier():
    return TelegramNotifier(bot_token='fake', chat_id='dm', alert_chat_id='dm', report_chat_id='channel')


def renderer(monkeypatch, result=None, error=None):
    fn = Mock(return_value=result, side_effect=error)
    monkeypatch.setitem(sys.modules, 'src.analytics.market_report_image', SimpleNamespace(render_market_report_image=fn))
    return fn


@pytest.mark.asyncio
@pytest.mark.parametrize('status,data,error,expected', [
    (200, {'ok': True}, None, 'accepted'),
    (200, {'ok': False}, None, 'rejected'),
    (500, {'ok': True}, None, 'unknown'),
    (200, [], None, 'unknown'),
    (200, None, ValueError('json'), 'unknown'),
    (200, None, TimeoutError(), 'unknown'),
])
async def test_photo_is_single_multipart_request_and_never_text_fallback(tmp_path, png, status, data, error, expected):
    n = notifier()
    response = AsyncMock(status=status)
    response.__aenter__.return_value = response
    response.json.return_value = data
    response.json.side_effect = error
    n._session = Mock(closed=False)
    n._session.post.return_value = response
    store = ReportStore(tmp_path/'r.db')
    args = ('market', 'morning', DAY, 'snapshot', '<b>시장</b>')
    assert await n.publish_report(*args, store=store, photo=png, caption='시장 & 확인') == expected
    assert await n.publish_report(*args, store=ReportStore(store.path), photo=png, caption='different') == 'already_consumed'
    n._session.post.assert_called_once()
    call = n._session.post.call_args
    assert call.args[0].endswith('/sendPhoto')
    assert 'json' not in call.kwargs and call.kwargs['timeout'].total == 30
    assert call.kwargs['allow_redirects'] is False
    # Serialize the actual aiohttp multipart payload, not a mocked photo helper.
    payload = call.kwargs['data']()
    class Writer:
        def __init__(self): self.parts = []
        async def write(self, data): self.parts.append(data)
    writer = Writer()
    await payload.write(writer)
    wire = b''.join(writer.parts)
    assert png in wire and '시장 & 확인'.encode() in wire
    assert b'name="caption"' in wire and b'name="chat_id"' in wire
    assert b'image/png' in wire and b'parse_mode' not in wire
    receipt = store.receipt('market', 'morning', DAY)
    assert receipt['status'] == expected and receipt['body'] == args[4]
    assert receipt['media_type'] == 'image/png'
    assert receipt['caption'] == '시장 & 확인'
    assert receipt['image_sha256'] == hashlib.sha256(png).hexdigest()
    assert 'png' not in receipt
    assert store.archived_photo('market', 'morning', DAY) == png


def test_media_claim_atomic_restart_and_immutable(tmp_path, png):
    path = tmp_path/'r.db'
    def claim(i):
        return ReportStore(path).claim('market', 'morning', DAY, str(i), str(i), photo=png, caption=str(i))
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(8))) == 1
    store = ReportStore(path)
    r = store.receipt('market', 'morning', DAY)
    assert r['source_snapshot_id'] == r['body'] == r['caption']
    assert r['status'] == 'attempted'
    assert store.archived_photo('market', 'morning', DAY) == png
    assert not claim(99)
    assert store.receipt('market', 'morning', DAY) == r


def test_media_insert_failure_rolls_back_slot_and_body(tmp_path, png):
    store = ReportStore(tmp_path/'r.db')
    store.receipt('market', 'morning', DAY)
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER reject_media BEFORE INSERT ON media_payloads BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.claim('market', 'morning', DAY, 's', 'body', photo=png, caption='caption')
    assert store.receipt('market', 'morning', DAY) is None
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM payloads').fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['bytes', 'oversize', 'caption', 'astral', 'private'])
async def test_invalid_photo_or_private_audience_uses_text_before_any_photo_attempt(tmp_path, png, bad):
    n = notifier()
    n._send_report_photo_once = AsyncMock(return_value='accepted')
    n._send_report_once = AsyncMock(return_value='accepted')
    photo = b'not png' if bad == 'bytes' else png
    if bad == 'oversize': photo = png + b'x' * (4 * 1024 * 1024)
    caption = 'x' * 901 if bad == 'caption' else ('😀' * 451 if bad == 'astral' else 'caption')
    audience, kind = ('engine', 'daily') if bad == 'private' else ('market', 'morning')
    store = ReportStore(tmp_path/'r.db')
    assert await n.publish_report(audience, kind, DAY, 's', 'body', store=store, photo=photo, caption=caption) == 'accepted'
    n._send_report_photo_once.assert_not_awaited()
    n._send_report_once.assert_awaited_once()
    r = store.receipt(audience, kind, DAY)
    assert r['media_type'] == 'text/html' and r['caption'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, RuntimeError('render failed'), ImportError('optional')])
async def test_renderer_failure_falls_back_once(tmp_path, monkeypatch, failure):
    fn = renderer(monkeypatch, error=failure)
    n = notifier()
    n._send_report_once = AsyncMock(return_value='accepted')
    n._send_report_photo_once = AsyncMock()
    store = ReportStore(tmp_path/'r.db')
    assert await ReportSchedule(store, n).tick(NOW, True, MARKET, '') == 'accepted'
    fn.assert_called_once()
    n._send_report_once.assert_awaited_once()
    n._send_report_photo_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_renderer_result_is_pretransport_text_fallback(tmp_path, monkeypatch):
    renderer(monkeypatch, object())
    n = notifier()
    n._send_report_once = AsyncMock(return_value='accepted')
    n._send_report_photo_once = AsyncMock()
    assert await ReportSchedule(ReportStore(tmp_path/'r.db'), n).tick(NOW, True, MARKET, '') == 'accepted'
    n._send_report_once.assert_awaited_once()
    n._send_report_photo_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_market_photo_archive_is_actual_caption_and_not_forecast(tmp_path, monkeypatch, png):
    fn = renderer(monkeypatch, SimpleNamespace(png=png, caption='실제 캡션', width=720, height=1000, plain_text='보고 본문'))
    n = notifier()
    n._send_report_photo_once = AsyncMock(return_value='accepted')
    n._send_report_once = AsyncMock(return_value='accepted')
    store = ReportStore(tmp_path/'r.db')
    def record(**kwargs): return record_morning_brief_dispatch(**kwargs, archive_dir=tmp_path/'archive')
    s = ReportSchedule(store, n, record)
    assert await s.tick(NOW, True, MARKET, '') == 'accepted'
    r = store.receipt('market', 'morning', DAY)
    paths = list((tmp_path/'archive').glob('*.json'))
    dispatch = json.loads(paths[0].read_text())['dispatch'][0]
    assert dispatch['sent_text'] == '실제 캡션'
    assert dispatch['media_type'] == 'image/png' and dispatch['image_sha256'] == r['image_sha256']
    assert dispatch['semantic_report_body'] == r['body'] == fn.call_args.args[0]
    assert dispatch['brief_match'] == 'none' and dispatch['brief_ref'] is None
    generator = DailyReportGenerator.__new__(DailyReportGenerator)
    brief, sent, ref, reason = generator._resolve_brief_for_eval(DAY, tmp_path/'absent', tmp_path/'archive')
    assert sent and brief['claims'] == {} and ref is None
    assert await s.tick(NOW.replace(hour=21, minute=0), True, MARKET, '개인') == 'accepted'
    fn.assert_called_once()
    n._send_report_photo_once.assert_awaited_once()
    n._send_report_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_renderer_drains_before_after_lock_release(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    def render(text):
        entered.set()
        assert release.wait(5)
        return None
    monkeypatch.setitem(sys.modules, 'src.analytics.market_report_image', SimpleNamespace(render_market_report_image=render))
    now = NOW.replace(hour=16, minute=40)
    market = MarketSnapshot(sectors=({'name': '반도체', 'r1': 1.5, 'as_of': DAY, 'source': 'etf'},), kr_basis_date=DAY)
    store = ReportStore(tmp_path/'r.db')
    n = notifier()
    n._send_report_once = AsyncMock(return_value='accepted')
    task = asyncio.create_task(ReportSchedule(store, n).tick(now, True, market, ''))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert await ReportSchedule(ReportStore(store.path), n).tick(now, True, market, '') == 'input_collecting'
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    n._send_report_once.assert_not_awaited()
    with store.input_guard(DAY) as acquired: assert acquired


@pytest.mark.asyncio
async def test_notifier_preflight_fallback_archive_uses_persisted_text(tmp_path, monkeypatch, png):
    renderer(monkeypatch, SimpleNamespace(png=png, caption='😀' * 451))
    n = notifier()
    n._send_report_once = AsyncMock(return_value='accepted')
    n._send_report_photo_once = AsyncMock()
    store = ReportStore(tmp_path/'r.db')
    recorder = Mock()
    assert await ReportSchedule(store, n, recorder).tick(NOW, True, MARKET, '') == 'accepted'
    n._send_report_photo_once.assert_not_awaited()
    r = store.receipt('market', 'morning', DAY)
    assert r['media_type'] == 'text/html'
    assert recorder.call_args.kwargs['sent_text'] == r['body']
    assert 'image_sha256' not in recorder.call_args.kwargs


@pytest.mark.asyncio
async def test_photo_cancellation_spends_slot_without_retry(tmp_path, png):
    n = notifier()
    entered = asyncio.Event()
    async def transport(*args):
        entered.set()
        await asyncio.Event().wait()
    n._send_report_photo_once = AsyncMock(side_effect=transport)
    n._send_report_once = AsyncMock()
    store = ReportStore(tmp_path/'r.db')
    args = ('market', 'morning', DAY, 's', 'body')
    task = asyncio.create_task(n.publish_report(*args, store=store, photo=png, caption='caption'))
    await asyncio.wait_for(entered.wait(), 2)
    assert store.receipt('market', 'morning', DAY)['status'] == 'attempted'
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert await n.publish_report(*args, store=ReportStore(store.path), photo=png, caption='caption') == 'already_consumed'
    n._send_report_photo_once.assert_awaited_once()
    n._send_report_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_media_claim_drains_before_after_lock_release(tmp_path, monkeypatch, png):
    renderer(monkeypatch, SimpleNamespace(png=png, caption='caption'))
    entered, release = Event(), Event()
    store = ReportStore(tmp_path/'r.db')
    real_claim = store.claim
    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_claim(*args, **kwargs)
    monkeypatch.setattr(store, 'claim', paused)
    now = NOW.replace(hour=16, minute=40)
    market = MarketSnapshot(sectors=({'name': '반도체', 'r1': 1.5, 'as_of': DAY, 'source': 'etf'},), kr_basis_date=DAY)
    n = notifier()
    n._send_report_photo_once = AsyncMock()
    task = asyncio.create_task(ReportSchedule(store, n).tick(now, True, market, ''))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        other = ReportSchedule(ReportStore(store.path), n)
        assert await other.tick(now, True, market, '') == 'input_collecting'
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
    assert await other.tick(now, True, market, '') == 'already_consumed'
    assert store.archived_photo('market', 'after', DAY) == png
    n._send_report_photo_once.assert_not_awaited()


def test_legacy_payload_remains_semantic_body_with_additive_media_table(tmp_path):
    path = tmp_path/'r.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE slots (audience TEXT, kind TEXT, report_date TEXT, source_snapshot_id TEXT, status TEXT, PRIMARY KEY(audience,kind,report_date))')
        db.execute('CREATE TABLE payloads (audience TEXT, kind TEXT, report_date TEXT, body TEXT, PRIMARY KEY(audience,kind,report_date))')
        db.execute('INSERT INTO slots VALUES (?,?,?,?,?)', ('market', 'morning', DAY, 's', 'accepted'))
        db.execute('INSERT INTO payloads VALUES (?,?,?,?)', ('market', 'morning', DAY, '<b>legacy</b>'))
    r = ReportStore(path).receipt('market', 'morning', DAY)
    assert r['body'] == '<b>legacy</b>' and r['media_type'] == 'text/html'
    assert r['image_sha256'] is None and r['caption'] is None
