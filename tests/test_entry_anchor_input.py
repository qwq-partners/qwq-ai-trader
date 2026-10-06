"""Anchor fetch failures distinguish transient transport from invalid input."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.observation.entry_anchor_input import AnchorClient, InputInvalid
from src.observation.toss_positions import InputUnavailable


NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


class Response:
    def __init__(self, body, *, status=200):
        self.body, self.status = body, status
        self.headers = {'Content-Encoding':'identity', 'Content-Length':str(len(body))}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, _size):
        yield self.body


class Session:
    _retry_connection = True

    def __init__(self, response=None, *, error=None):
        self.response, self.error, self.closed = response, error, False

    def get(self, *_args, **_kwargs):
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self):
        self.closed = True


def projection(*, observed_at=NOW):
    return dict(schema_version='entry-anchor-projection-v2', evaluation_epoch='epoch-1',
        study_sha256='b' * 64, capture_id='capture-1', observed_at=observed_at.isoformat(),
        capture_closed=False, journal_sealed=False, source_record_count=0, complete=False,
        incomplete_reasons=['engine_source_not_final'], dropped_records=0, records=[],
        selection=dict(rule='first_three_in_returned_order', returned_candidate_count=0,
                       selected_candidate_ids=[]))


@pytest.mark.parametrize('body', [
    b'{',
    json.dumps(dict(projection(), schema_version='wrong')).encode(),
])
def test_anchor_client_marks_malformed_or_schema_input_invalid(body):
    client = AnchorClient(session_factory=lambda:Session(Response(body)), clock=lambda:100)
    with pytest.raises(InputInvalid, match='input_invalid'):
        asyncio.run(client.fetch())


def test_anchor_client_keeps_transport_failure_transient():
    client = AnchorClient(session_factory=lambda:Session(error=OSError('synthetic transport')), clock=lambda:100)
    with pytest.raises(InputUnavailable, match='input_unavailable'):
        asyncio.run(client.fetch())


def test_anchor_client_stale_projection_reaches_immediate_runtime_rejection():
    from src.observation.toss_ws_service import _input

    client = AnchorClient(session_factory=lambda:Session(Response(json.dumps(
        projection(observed_at=NOW - timedelta(seconds=6))).encode())), clock=lambda:100)
    value = asyncio.run(client.fetch())
    policy = dict(evaluation_epoch='epoch-1', engine_study_sha256='b' * 64,
                  start_at=NOW.isoformat(), scan_until=(NOW + timedelta(seconds=20)).isoformat())
    with pytest.raises(ValueError, match='engine_identity_or_time_invalid'):
        _input(value, policy, NOW)
