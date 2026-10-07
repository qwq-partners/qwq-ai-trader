"""Current-process readiness only; no account or candidate data is exposed."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src.dashboard.kr_api import KRAPIHandler


def request(**changes):
    values = dict(remote='127.0.0.1', query_string='', headers={
        'Host': '127.0.0.1:8080', 'X-QWQ-Observation': '1'})
    values.update(changes)
    return SimpleNamespace(**values)


def handler(*, ended=False, closed=False):
    runtime = SimpleNamespace(
        plan=SimpleNamespace(study_sha256='a' * 64),
        buffer=SimpleNamespace(evaluation_epoch='synthetic-window', scan_scope='window',
                               ended=ended, _capture_closed=closed),
        journal=SimpleNamespace(_header={'capture_id': 'synthetic-current-process'},
                                failure_reason=None, _thread=SimpleNamespace(is_alive=lambda: True)))
    return KRAPIHandler(SimpleNamespace(bot=SimpleNamespace(_entry_observation_runtime=runtime)))


@pytest.mark.asyncio
async def test_readiness_uses_live_runtime_not_anchor_projection():
    h = handler()
    response = await h.get_entry_observation_readiness(request())
    assert response.status == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert json.loads(response.text) == {
        'schema_version': 'entry-observation-readiness-v1', 'study_sha256': 'a' * 64,
        'evaluation_epoch': 'synthetic-window', 'capture_id': 'synthetic-current-process',
        'scan_scope': 'window', 'capture_closed': False}
    # The old first-scan projection must still reject the window contract.
    assert (await h.get_entry_anchors(request())).status == 503


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {'remote': '192.0.2.1'}, {'query_string': 'refresh=1'},
    {'headers': {'Host': 'localhost:8080', 'X-QWQ-Observation': '1'}},
    {'headers': {'Host': '127.0.0.1:8080'}},
    *[{'headers': {'Host': '127.0.0.1:8080', 'X-QWQ-Observation': '1', k: 'x'}}
      for k in ('Origin', 'Forwarded', 'Via', 'X-Forwarded-Host')],
])
async def test_readiness_denies_remote_or_proxied_requests(changes):
    assert (await handler().get_entry_observation_readiness(request(**changes))).status == 403


@pytest.mark.asyncio
async def test_consumed_once_without_current_runtime_is_unavailable():
    h = KRAPIHandler(SimpleNamespace(bot=SimpleNamespace()))
    assert (await h.get_entry_observation_readiness(request())).status == 503


@pytest.mark.asyncio
@pytest.mark.parametrize('ended,closed', [(True, False), (False, True), (True, True)])
async def test_ended_or_sealed_capture_is_not_ready(ended, closed):
    response = await handler(ended=ended, closed=closed).get_entry_observation_readiness(request())
    assert json.loads(response.text)['capture_closed'] is True


@pytest.mark.asyncio
async def test_failed_current_journal_is_not_ready(tmp_path, monkeypatch):
    from test_entry_observation_runtime import load, NOW, owner
    from test_signal_window_observation import window_plan
    from src.analytics.entry_observation_runtime import ObservationRuntime
    plan, _ = load(tmp_path, window_plan)
    bot = owner()
    runtime = await ObservationRuntime.install(bot, plan, now=lambda: NOW)
    h = KRAPIHandler(SimpleNamespace(bot=bot))
    try:
        assert (await h.get_entry_observation_readiness(request())).status == 200
        def fail(_rows):
            raise OSError('synthetic disk failure')
        monkeypatch.setattr(runtime.journal, '_write_batch', fail)
        runtime.buffer.publish({'kind': 'quote_subscription', 'status': 'connected'})
        async def failed():
            while runtime.journal.failure_reason is None:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(failed(), 2)
        assert runtime.journal.failure_reason == 'journal_writer_failed'
        assert (await h.get_entry_observation_readiness(request())).status == 503
    finally:
        await runtime.close('synthetic_test_finished')
