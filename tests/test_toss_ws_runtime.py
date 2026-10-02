"""실접속 조립 경로는 합성 승인/루프백 대역/가짜 외부 소켓으로만 검증한다."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import json
from types import SimpleNamespace

import pytest

from test_toss_live_authority import authority_fixture, plan_document, raw

START = datetime(2026, 9, 16, tzinfo=timezone.utc)
WS_SHA = "130251057fd9535a3e276099f9166b445f8c51f505f30540758e4b209231282e"


def ws_plan():
    p = plan_document()
    p["schema_version"] = 2
    p["websocket"] = dict(endpoint="wss://openapi-ws.tossinvest.com/ws/v1",
        asyncapi_version="1.2.2", asyncapi_sha256=WS_SHA, channel="orderbook:kr",
        evaluation_epoch="epoch-1", request_id="probe-1", engine_study_sha256="b" * 64,
        start_at=START.isoformat(), scan_until=(START + timedelta(seconds=20)).isoformat(),
        end_at=(START + timedelta(seconds=50)).isoformat(), max_frames=1000,
        max_source_age_seconds=1, poll_seconds=.1)
    return p


def authority(tmp_path, monkeypatch):
    api, options, grant, stamp, ticks = authority_fixture(tmp_path, monkeypatch)
    value = ws_plan()
    plan = api.ObservationPlan.from_bytes(raw(value))
    grant.update(schema_version=2, plan_raw_hash=plan.raw_hash, plan_canonical_hash=plan.canonical_hash)
    grant["capabilities"]["websocket"] = True
    options["registry_path"].write_bytes(raw(dict(schema_version=1, grants=[grant])))
    options["plan_path"].write_bytes(raw(value))
    return api.load_authority(**options), stamp, ticks, options


def test_v1_query_permission_never_authorizes_websocket(tmp_path, monkeypatch):
    api, options, *_ = authority_fixture(tmp_path, monkeypatch)
    a = api.load_authority(**options)
    with pytest.raises(api.ApprovalError):
        a.require("websocket", deadline=110)


def test_explicit_v2_ws_permission_is_bound_to_window_and_grant(tmp_path, monkeypatch):
    a, stamp, ticks, _ = authority(tmp_path, monkeypatch)
    assert a.require("websocket", deadline=160) == 150
    stamp[0] += timedelta(seconds=50)
    with pytest.raises(Exception):
        a.require("websocket", deadline=160)


@pytest.mark.parametrize("field,value", [
    ("endpoint", "wss://example.org/ws"), ("channel", "personal:order"),
    ("max_frames", True), ("max_frames", 50001), ("asyncapi_sha256", "0" * 64),
    ("end_at", (START + timedelta(hours=2)).isoformat()), ("poll_seconds", 0),
])
def test_ws_plan_rejects_wrong_destination_or_unbounded_contract(field, value):
    from src.data.providers.toss.approval import ObservationPlan, ApprovalError
    p = ws_plan()
    p["websocket"][field] = value
    with pytest.raises(ApprovalError):
        ObservationPlan.from_bytes(raw(p))


def api_module():
    name = "src.observation.entry_anchor_input"
    assert importlib.util.find_spec(name) is not None, "실행 중 후보 전달 경로 미구현"
    return importlib.import_module(name)


def runtime():
    from src.analytics.entry_observation import EntryObservationBuffer
    b = EntryObservationBuffer(evaluation_epoch="epoch-1", capacity=20)
    b.publish(dict(kind="scan", scan_id="scan-1", observed_at=START.isoformat(),
        route_origin="live_screening", population_scope="returned_screen_candidates",
        candidates=[dict(candidate_id="scan-1:005930", symbol="005930", score=99, secret="not-exported")]))
    b.publish(dict(kind="ws_quote", ask="10000", account="not-exported"))
    b.publish(dict(kind="signal", candidate_id="scan-1:005930", signal_id="signal-1",
                   observed_at=(START + timedelta(seconds=1)).isoformat()))
    b.publish(dict(kind="order_ready", signal_id="signal-1", symbol="005930", order_id="order-1",
                   requested_quantity=3, observed_at=(START + timedelta(seconds=2)).isoformat(),
                   capital_snapshot={"cash":"not-exported"}))
    return SimpleNamespace(buffer=b, plan=SimpleNamespace(study_sha256="b" * 64),
        journal=SimpleNamespace(_header={"capture_id":"capture-1"}), result=None)


def projection(closed=True):
    r = runtime()
    if closed:
        r.buffer._capture_closed = True
        r.result = {"sealed":True,"fsync_confirmed":True,"error":None}
    return api_module().project_anchors(r, now=START + timedelta(seconds=51))


def test_projection_uses_original_order_and_excludes_account_or_quote_fields():
    r = runtime()
    before = r.buffer.export()
    p = api_module().project_anchors(r, now=START + timedelta(seconds=3))
    assert p["schema_version"] == "entry-anchor-projection-v2"
    assert [v["source_sequence"] for v in p["records"]] == [1, 3, 4]
    assert [v["sequence"] for v in p["records"]] == [1, 2, 3]
    assert "not-exported" not in json.dumps(p)
    assert p["complete"] is False and p["capture_closed"] is False
    assert r.buffer.export() == before


def test_only_final_successful_seal_marks_engine_projection_complete():
    r = runtime()
    r.buffer._capture_closed = True
    assert api_module().project_anchors(r, now=START + timedelta(seconds=3))["complete"] is False
    r.result = {"sealed":True,"fsync_confirmed":True,"error":None}
    assert api_module().project_anchors(r, now=START + timedelta(seconds=3))["complete"] is True
    r.result["error"] = "failed"
    assert api_module().project_anchors(r, now=START + timedelta(seconds=3))["complete"] is False


@pytest.mark.asyncio
async def test_candidate_endpoint_is_loopback_only_and_does_not_fetch_broker():
    from src.dashboard.kr_api import KRAPIHandler
    handler = KRAPIHandler(SimpleNamespace(bot=SimpleNamespace(_entry_observation_runtime=runtime())))
    assert hasattr(handler, "get_entry_anchors"), "후보 조회 경로 미구현"
    denied = await handler.get_entry_anchors(SimpleNamespace(remote="8.8.8.8", headers={}))
    assert denied.status == 403
    forwarded = await handler.get_entry_anchors(SimpleNamespace(remote="127.0.0.1", headers={"X-Forwarded-For":"8.8.8.8"}))
    assert forwarded.status == 403
    allowed = await handler.get_entry_anchors(SimpleNamespace(remote="127.0.0.1",
        headers={"Host":"127.0.0.1:8080", "X-QWQ-Observation":"1"}))
    assert allowed.status == 200
    assert json.loads(allowed.text)["records"][0]["scan_id"] == "scan-1"


def ws_module():
    name = "src.data.providers.toss.ws_transport"
    assert importlib.util.find_spec(name) is not None, "인증된 WS 접속 경로 미구현"
    return importlib.import_module(name)


class FakeWS:
    def __init__(self):
        self.closed = False
    async def close(self): self.closed = True
    async def send_str(self, text): pass
    async def receive_str(self): return '{"type":"pong"}'


class Session:
    _retry_connection = True
    def __init__(self):
        self.calls = []
        self.closed = False
        self.ws = FakeWS()
    async def ws_connect(self, url, **kw):
        self.calls.append((url, kw))
        return self.ws
    async def close(self): self.closed = True


@pytest.mark.asyncio
async def test_transport_connects_once_to_fixed_endpoint_and_no_implicit_retries(tmp_path, monkeypatch):
    a, *_ = authority(tmp_path, monkeypatch)
    session = Session()
    transport = ws_module().TossWebSocketTransport(a, session_factory=lambda **kw:session)
    ws = await transport.connect("synthetic-token", deadline=150)
    url, options = session.calls[0]
    assert url == "wss://openapi-ws.tossinvest.com/ws/v1"
    assert options["headers"] == {"Authorization":"Bearer synthetic-token"}
    assert options["max_msg_size"] == 65536 and options["ssl"] is True
    assert session._retry_connection is False
    with pytest.raises(Exception):
        await transport.connect("synthetic-token", deadline=150)
    await ws.close()
    await transport.close()
    assert session.closed and session.ws.closed


@pytest.mark.asyncio
async def test_redirect_is_rejected_before_a_second_handshake():
    closed = []
    response = SimpleNamespace(status=302, close=lambda:closed.append(True))
    async def send(request): return response
    with pytest.raises(Exception):
        await ws_module().reject_ws_redirect(SimpleNamespace(url="wss://openapi-ws.tossinvest.com/ws/v1", method="GET"), send)
    assert closed == [True]


def service_module():
    name = "src.observation.toss_ws_service"
    assert importlib.util.find_spec(name) is not None, "후보 수집 실행/저장 경로 미구현"
    return importlib.import_module(name)


def test_artifact_requires_final_record_and_never_overwrites(tmp_path):
    mod = service_module()
    path = tmp_path / "capture.jsonl"
    journal = mod.CaptureArtifact(path, max_bytes=200000, plan_hash="c" * 64)
    journal.open()
    with pytest.raises(ValueError):
        mod.read_capture_artifact(path, max_bytes=200000, plan_hash="c" * 64)
    with pytest.raises(FileExistsError):
        mod.CaptureArtifact(path, max_bytes=200000, plan_hash="c" * 64).open()
    journal.finish({"engine":projection(), "toss":{"synthetic":True}, "as_of":START.isoformat()})
    journal.close()
    assert mod.read_capture_artifact(path, max_bytes=200000, plan_hash="c" * 64)["engine"]["capture_id"] == "capture-1"


def test_artifact_partial_write_and_tampering_never_becomes_complete(tmp_path):
    mod = service_module()
    path = tmp_path / "capture.jsonl"
    journal = mod.CaptureArtifact(path, max_bytes=200000, plan_hash="c" * 64)
    journal.open()
    journal.finish({"test":"untampered"})
    journal.close()
    data = path.read_bytes()
    path.write_bytes(data.replace(b'untampered', b'alteredxxx'))
    with pytest.raises(ValueError):
        mod.read_capture_artifact(path, max_bytes=200000, plan_hash="c" * 64)


class ServiceHarness:
    """실제 수신 루프, 승인 객체, 저장기를 쓰되 외부 IO만 합성한다."""
    def __init__(self, a, stamp, ticks, *, problem=None):
        self.a, self.stamp, self.ticks, self.problem = a, stamp, ticks, problem
        self.events, self.fetches, self.messages = [], 0, 0
        self.at(3)
        self.deployment = SimpleNamespace(load=lambda:a, preflight_timeout_seconds=1)
        self.components = SimpleNamespace(owner=self, tokens=self, transport=self, oauth=None)

    def at(self, seconds):
        self.stamp[0] = START + timedelta(seconds=seconds)
        self.ticks[0] = 100 + seconds

    async def fetch(self):
        self.fetches += 1
        closed = self.stamp[0] >= START + timedelta(seconds=50)
        p = projection(closed=closed)
        p['observed_at'] = self.stamp[0].isoformat()
        if self.problem == 'empty':
            p['records'] = p['records'][:1]
            p['records'][0]['candidates'] = []
            p['selection'].update(returned_candidate_count=0, selected_candidate_ids=[])
        elif self.problem == 'epoch':
            p['evaluation_epoch'] = 'wrong'
        elif self.problem == 'late':
            self.at(21)
            p['observed_at'] = self.stamp[0].isoformat()
        elif self.problem == 'unsealed':
            p.update(journal_sealed=False, complete=False, incomplete_reasons=['engine_source_not_final'])
            if closed:
                self.at((self.stamp[0] - START).total_seconds() + 1)
        elif self.problem == 'changed' and self.fetches > 1:
            p['capture_id'] = 'another-capture'
        return p

    async def start(self):
        self.events.append('sender_lock')
        if self.problem == 'lock':
            raise ValueError('synthetic_lock_busy')

    async def get_websocket_token(self, **kw):
        self.events.append('token')
        return 'synthetic-token'

    async def connect(self, token, **kw):
        self.events.append('connect')
        return self

    async def send_str(self, data):
        self.events.append('declaration')

    async def receive_str(self):
        self.messages += 1
        if self.problem == 'disconnect':
            raise ConnectionError('synthetic')
        if self.problem in ('cancel', 'changed'):
            await asyncio.sleep(30)
        if self.messages == 1:
            self.at(3)
            return json.dumps({'type':'subscriptions', 'id':'probe-1',
                'subscribed':['orderbook:kr:005930'], 'rejected':[]})
        self.at(50)
        raise asyncio.TimeoutError()

    async def close(self):
        self.events.append('close')

    async def run(self):
        return await service_module().run_ws_service(deployment=self.deployment,
            claim_start=lambda _:self.events.append('claim'), stop_event=asyncio.Event(),
            anchor_factory=lambda:self, components_factory=lambda _:self.components)


@pytest.mark.asyncio
async def test_service_connects_after_lock_and_token_then_saves_sealed_result(tmp_path, monkeypatch):
    a, stamp, ticks, _ = authority(tmp_path, monkeypatch)
    monkeypatch.setenv('TOSS_API', '1')
    h = ServiceHarness(a, stamp, ticks)
    assert await h.run() == 0
    assert h.events[:5] == ['claim','sender_lock','token','connect','declaration']
    artifact = service_module().read_capture_artifact(a.grant.ledger_path,
        max_bytes=1000000, plan_hash=a.plan.canonical_hash)
    assert artifact['service_complete'] is True
    assert artifact['engine']['journal_sealed'] is True
    assert artifact['toss']['stop_reason'] == 'window_ended'
    assert artifact['profit_comparison_available'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['empty', 'epoch', 'late', 'lock', 'disconnect', 'unsealed', 'changed'])
async def test_service_failure_is_incomplete_and_never_reconnects(tmp_path, monkeypatch, problem):
    a, stamp, ticks, _ = authority(tmp_path, monkeypatch)
    monkeypatch.setenv('TOSS_API', '1')
    h = ServiceHarness(a, stamp, ticks, problem=problem)
    assert await asyncio.wait_for(h.run(), 3) == 1
    result = service_module().read_capture_artifact(a.grant.ledger_path,
        max_bytes=1000000, plan_hash=a.plan.canonical_hash)
    assert result['service_complete'] is False
    assert h.events.count('connect') <= 1
    if problem in ('empty', 'epoch', 'late', 'lock'):
        assert 'token' not in h.events


@pytest.mark.asyncio
async def test_service_cancel_closes_socket_and_preserves_incomplete_evidence(tmp_path, monkeypatch):
    a, stamp, ticks, _ = authority(tmp_path, monkeypatch)
    monkeypatch.setenv('TOSS_API', '1')
    h = ServiceHarness(a, stamp, ticks, problem='cancel')
    task = asyncio.create_task(h.run())
    async with asyncio.timeout(2):
        while 'declaration' not in h.events:
            await asyncio.sleep(.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = service_module().read_capture_artifact(a.grant.ledger_path,
        max_bytes=1000000, plan_hash=a.plan.canonical_hash)
    assert result['service_complete'] is False and result['service_reason'] == 'cancelled'
    assert 'close' in h.events


@pytest.mark.asyncio
async def test_default_off_never_reads_approval_or_creates_files(tmp_path, monkeypatch):
    monkeypatch.delenv('TOSS_API', raising=False)
    def forbidden(*args): pytest.fail('default off side effect')
    assert await service_module().run_ws_service(deployment=SimpleNamespace(load=forbidden),
        claim_start=forbidden, anchor_factory=forbidden, components_factory=forbidden) == 0
    assert not list(tmp_path.iterdir())


def test_launcher_service_choice_requires_explicit_ws_plan(tmp_path, monkeypatch):
    from src.observation.toss_deployment import service_for
    a, *_ = authority(tmp_path, monkeypatch)
    assert service_for(a) is service_module().run_ws_service
    # Merely passing a dictionary cannot manufacture live authority.
    with pytest.raises(Exception):
        service_for({'schema_version':2})


def test_ws_deployment_policy_requires_short_grant_and_finalization_window(tmp_path, monkeypatch):
    from src.observation.toss_deployment import _validate_policy
    a, *_ = authority(tmp_path, monkeypatch)
    document = dict(plan_raw_hash=a.plan.raw_hash, plan_canonical_hash=a.plan.canonical_hash,
        retention_at=(START + timedelta(seconds=60, days=30)).isoformat())
    _validate_policy(a, document)
    document['retention_at'] = START.isoformat()
    with pytest.raises(Exception):
        _validate_policy(a, document)


def test_report_cli_reads_sealed_artifact_and_preserves_service_failure(tmp_path, capsys):
    from scripts.report_toss_candidate_observation import main
    from src.data.providers.toss.orderbook_stream import TossOrderbookCapture
    cap = TossOrderbookCapture(symbols=['005930'], request_id='probe-1', evaluation_epoch='epoch-1',
        start_at=START, end_at=START + timedelta(seconds=50))
    cap.feed(json.dumps(dict(type='subscriptions', id='probe-1',
        subscribed=['orderbook:kr:005930'], rejected=[])), received_at=START + timedelta(seconds=1))
    cap.stop('window_ended')
    engine = projection()
    # Fixture capture identity/window use explicit values rather than live data.
    data = cap.export()
    data.update(evaluation_epoch='epoch-1', start_at=START.isoformat(),
        end_at=(START + timedelta(seconds=50)).isoformat(),
        acknowledged_at=(START + timedelta(seconds=1)).isoformat())
    path = tmp_path / 'sealed.jsonl'
    artifact = service_module().CaptureArtifact(path, max_bytes=1000000, plan_hash='c' * 64)
    artifact.open()
    artifact.finish(dict(engine=engine, toss=data, as_of=(START + timedelta(seconds=51)).isoformat(),
        service_complete=False, service_reason='cleanup_failed', profit_comparison_available=False))
    artifact.close()
    assert main(['--artifact',str(path),'--plan-sha256','c' * 64]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['service_complete'] is False
    assert result['service_reason'] == 'cleanup_failed'


def test_report_cli_accepts_sealed_early_frame_limit_artifact_and_marks_it_incomplete(tmp_path, capsys):
    from scripts.report_toss_candidate_observation import main

    capture_start = datetime(2026, 10, 2, 0, 15, tzinfo=timezone.utc)
    capture_end = datetime(2026, 10, 2, 0, 45, tzinfo=timezone.utc)
    reported_at = datetime(2026, 10, 2, 0, 24, 35, 678000, tzinfo=timezone.utc)
    quote_base = datetime(2026, 10, 2, 0, 24, tzinfo=timezone.utc)
    records = [dict(source='TOSS_WS_ORDERBOOK_KR', market_basis='KRX_NXT_CONSOLIDATED',
        delivery='LOSSY', source_sequence=None, symbol='005930', received_index=index,
        received_at=(quote_base + timedelta(microseconds=index)).isoformat(),
        source_as_of=(quote_base + timedelta(microseconds=index - 1)).isoformat(),
        ask='10000', bid='9990', ask_size='10', bid_size='20', quality_issues=[], kis_executable=False)
        for index in range(9, 10001)]
    engine = dict(schema_version=1, evaluation_epoch='epoch-early', complete=False,
        dropped_records=0, incomplete_reasons=['capture_open'], observed_at='2026-10-02T00:24:34+00:00',
        records=[dict(kind='scan', sequence=1, scan_id='scan-early', observed_at=capture_start.isoformat(),
            route_origin='live_screening', population_scope='returned_screen_candidates',
            candidates=[dict(candidate_id='scan-early:005930', symbol='005930')])])
    toss = dict(schema_version='toss-orderbook-capture-v1', evaluation_epoch='epoch-early',
        source='TOSS_WS_ORDERBOOK_KR', market_basis='KRX_NXT_CONSOLIDATED', delivery='LOSSY',
        stream_complete=None, request_id='probe-early', symbols=['005930'], start_at=capture_start.isoformat(),
        end_at=capture_end.isoformat(), max_source_age_seconds=1, max_frames=10000, received_frames=10000,
        acknowledged_at=(capture_start + timedelta(seconds=1)).isoformat(), acknowledged_symbols=['005930'],
        rejected_symbols=[], stop_reason='frame_limit', cleanup_failed=False, records=records)
    path = tmp_path / 'early-frame-limit.jsonl'
    artifact = service_module().CaptureArtifact(path, max_bytes=16 * 1024 * 1024, plan_hash='d' * 64)
    artifact.open()
    artifact.finish(dict(engine=engine, toss=toss, as_of=reported_at.isoformat(), service_complete=False,
        service_reason='engine_input_incomplete', profit_comparison_available=False))
    artifact.close()

    assert main(['--artifact', str(path), '--plan-sha256', 'd' * 64]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['service_complete'] is False
    assert result['profit_comparison_available'] is False
    assert result['kis_execution_evidence'] is False
    assert result['coverage'] == dict(planned_start_at=capture_start.isoformat(),
        planned_end_at=capture_end.isoformat(), reported_as_of=reported_at.isoformat(),
        last_received_quote_at=records[-1]['received_at'], stop_reason='frame_limit',
        received_frames=10000, max_frames=10000, quote_count=9992, cleanup_failed=False)
    assert {'engine_capture_incomplete', 'toss_capture_incomplete'} <= set(
        result['candidates'][0]['first_after_scan']['reasons'])


@pytest.mark.asyncio
async def test_stop_revokes_authority_while_waiting_for_token(tmp_path, monkeypatch):
    a, stamp, ticks, _ = authority(tmp_path, monkeypatch)
    monkeypatch.setenv('TOSS_API', '1')
    h = ServiceHarness(a, stamp, ticks)
    waiting, stop = asyncio.Event(), asyncio.Event()
    async def delayed_token(**kw):
        waiting.set()
        await asyncio.sleep(30)
        pytest.fail('token continued after stop')
    h.get_websocket_token = delayed_token
    task = asyncio.create_task(service_module().run_ws_service(deployment=h.deployment,
        claim_start=lambda _:None, stop_event=stop, anchor_factory=lambda:h,
        components_factory=lambda _:h.components))
    await asyncio.wait_for(waiting.wait(), 2)
    stop.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    with pytest.raises(Exception):
        a.require('renewal', deadline=150)
    assert 'connect' not in h.events
    artifact = service_module().read_capture_artifact(a.grant.ledger_path,
        max_bytes=1000000, plan_hash=a.plan.canonical_hash)
    assert artifact['service_complete'] is False


def test_ws_approval_requires_grace_for_local_seal_fetch(tmp_path, monkeypatch):
    a, stamp, ticks, options = authority(tmp_path, monkeypatch)
    value = json.loads(options['registry_path'].read_bytes())
    value['grants'][0]['expires_at'] = a.plan.document['websocket']['end_at']
    options['registry_path'].write_bytes(raw(value))
    from src.data.providers.toss.approval import load_authority, ApprovalError
    with pytest.raises(ApprovalError):
        load_authority(**options)


@pytest.mark.parametrize('mutate', [
    lambda p:p['records'][1].update(candidate_id='other-scan:000660'),
    lambda p:p['records'][2].update(requested_quantity={'unexpected':'object'}),
    lambda p:p['records'][2].update(requested_quantity=True),
    lambda p:p['records'][1].pop('signal_id'),
    lambda p:p['records'][0]['candidates'][0].update(symbol=['005930']),
    lambda p:p['records'][2].update(observed_at=(START + timedelta(hours=1)).isoformat()),
])
def test_projection_rejects_broken_graph_before_service_success(mutate):
    p = projection()
    mutate(p)
    with pytest.raises(ValueError):
        api_module().validate_projection(p)


def test_failed_final_fsync_does_not_leave_successful_result(tmp_path, monkeypatch):
    mod = service_module()
    path = tmp_path / 'write-failed.jsonl'
    artifact = mod.CaptureArtifact(path, max_bytes=1000000, plan_hash='c' * 64)
    artifact.open()
    def disk_error(fd):
        raise OSError('synthetic_disk_failure')
    monkeypatch.setattr(mod.os, 'fsync', disk_error)
    with pytest.raises(OSError):
        artifact.finish({'service_complete':True})
    artifact.close()
    with pytest.raises(ValueError, match='artifact_unsealed'):
        mod.read_capture_artifact(path, max_bytes=1000000, plan_hash='c' * 64)


def many_candidate_runtime(count=20):
    r = runtime()
    items = r.buffer._records[0]['candidates']
    items.extend(dict(candidate_id=f'scan-1:{i:06}', symbol=f'{i:06}') for i in range(1, count))
    # 선택 밖 후보도 신호가 있을 수 있다. 투영 누락을 no_signal로 해석하면 안 된다.
    r.buffer.publish(dict(kind='signal', candidate_id='scan-1:000003', signal_id='outside',
        observed_at=(START+timedelta(seconds=3)).isoformat()))
    r.buffer.publish(dict(kind='order_ready', signal_id='outside', symbol='000003', order_id='outside-order',
        requested_quantity=7, observed_at=(START+timedelta(seconds=4)).isoformat()))
    return r


def test_real_sized_scan_keeps_all_candidates_and_projects_only_first_three():
    r = many_candidate_runtime()
    before = deepcopy(r.buffer._records)
    p = api_module().project_anchors(r, now=START+timedelta(seconds=5))
    assert len(p['records'][0]['candidates']) == 20
    assert p['selection']['selected_candidate_ids'] == ['scan-1:005930','scan-1:000001','scan-1:000002']
    assert p['selection']['returned_candidate_count'] == 20
    assert p['selection']['rule'] == 'first_three_in_returned_order'
    assert all(row.get('signal_id') != 'outside' for row in p['records'])
    assert r.buffer._records == before
    policy = ws_plan()['websocket']
    with pytest.raises(ValueError):
        service_module()._input(p, policy, START+timedelta(seconds=5))
    policy['candidate_selection_rule'] = 'first_three_in_returned_order'
    assert service_module()._input(p, policy, START+timedelta(seconds=5))[1] == ['005930','000001','000002']


def test_subset_report_retains_full_denominator_and_does_not_claim_missing_signals():
    from src.analytics.toss_candidate_observation import build_toss_candidate_report
    from src.data.providers.toss.orderbook_stream import TossOrderbookCapture
    r = many_candidate_runtime()
    r.buffer._capture_closed = True
    r.result = dict(sealed=True, fsync_confirmed=True, error=None)
    p = api_module().project_anchors(r, now=START+timedelta(seconds=51))
    c = TossOrderbookCapture(symbols=['005930','000001','000002'], request_id='probe-1',
        evaluation_epoch='epoch-1', start_at=START, end_at=START+timedelta(seconds=50))
    c.feed(json.dumps(dict(type='subscriptions',id='probe-1',
        subscribed=[f'orderbook:kr:{s}' for s in c.symbols],rejected=[])),received_at=START+timedelta(seconds=5))
    c.stop('window_ended')
    report = build_toss_candidate_report(p,c.export(),as_of=START+timedelta(seconds=51))
    assert len(report['candidates']) == 20
    assert report['population']['observed_subset_count'] == 3
    outside = report['candidates'][3]
    assert outside['engine_stage'] == 'outside_projected_subset'
    assert outside['first_after_scan']['reasons'] == ['outside_toss_capture_subset']
    assert outside['first_after_scan']['quote'] is None
    assert report['profit_comparison_available'] is False


def test_selection_rule_is_approved_and_projection_cannot_pick_a_later_winner():
    from src.data.providers.toss.approval import ObservationPlan
    plan = ws_plan()
    plan['websocket']['candidate_selection_rule'] = 'first_three_in_returned_order'
    ObservationPlan.from_bytes(raw(plan))
    p = api_module().project_anchors(many_candidate_runtime(), now=START+timedelta(seconds=5))
    p['selection']['selected_candidate_ids'][0] = 'scan-1:000010'
    with pytest.raises(ValueError):
        api_module().validate_projection(p)


def test_oversized_returned_population_still_fails_without_silent_truncation():
    with pytest.raises(ValueError):
        api_module().project_anchors(many_candidate_runtime(101), now=START+timedelta(seconds=5))


@pytest.mark.asyncio
async def test_service_seals_twenty_candidate_cohort_with_only_three_subscriptions(tmp_path, monkeypatch):
    original_plan = ws_plan
    def subset_plan():
        p = original_plan()
        p['websocket']['candidate_selection_rule'] = 'first_three_in_returned_order'
        return p
    monkeypatch.setattr(__import__(__name__), 'ws_plan', subset_plan)
    a, stamp, ticks, _ = authority(tmp_path, monkeypatch)
    monkeypatch.setenv('TOSS_API', '1')
    class ManyHarness(ServiceHarness):
        async def fetch(self):
            r = many_candidate_runtime()
            if self.stamp[0] >= START + timedelta(seconds=50):
                r.buffer._capture_closed = True
                r.result = dict(sealed=True, fsync_confirmed=True, error=None)
            return api_module().project_anchors(r, now=self.stamp[0])
        async def send_str(self, data):
            self.declaration = json.loads(data)
            await super().send_str(data)
        async def receive_str(self):
            self.messages += 1
            if self.messages == 1:
                self.at(5)
                return json.dumps(dict(type='subscriptions', id='probe-1',
                    subscribed=['orderbook:kr:005930','orderbook:kr:000001','orderbook:kr:000002'], rejected=[]))
            self.at(50)
            raise asyncio.TimeoutError()
    h = ManyHarness(a, stamp, ticks)
    h.at(5)
    assert await h.run() == 0
    artifact = service_module().read_capture_artifact(a.grant.ledger_path,
        max_bytes=1000000, plan_hash=a.plan.canonical_hash)
    assert artifact['service_complete'] is True
    assert len(artifact['engine']['records'][0]['candidates']) == 20
    assert artifact['toss']['symbols'] == ['005930','000001','000002']
    assert h.declaration[1]['codes'] == ['005930','000001','000002']


def test_legacy_projection_still_accepts_whole_small_cohort():
    p = projection(closed=True)
    p['schema_version'] = 'entry-anchor-projection-v1'
    p.pop('selection')
    api_module().validate_projection(p)
    assert service_module()._input(p, ws_plan()['websocket'], START+timedelta(seconds=51))[1] == ['005930']
