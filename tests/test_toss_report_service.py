"""Report helper framing and lifecycle use a single persistent sender."""
import asyncio
import importlib
from pathlib import Path
import pytest


def api():
    assert Path('src/observation/toss_report_helper.py').exists(), 'report helper missing'
    return importlib.import_module('src.observation.toss_report_helper')

@pytest.mark.asyncio
async def test_off_helper_does_not_open_stdio_or_load_authority(monkeypatch):
    monkeypatch.setenv('TOSS_API', '0')
    class Deployment:
        def load(self): pytest.fail('authority accessed while OFF')
    assert await api().run_report_helper(deployment=Deployment(), settings={}, claim_start=lambda _: pytest.fail('claim')) == 0

@pytest.mark.asyncio
async def test_frame_rejects_partial_and_oversized_request():
    module = api()
    for payload in (b'{}', b'x' * 16385 + b'\n'):
        reader = asyncio.StreamReader(limit=16385)
        reader.feed_data(payload)
        reader.feed_eof()
        with pytest.raises(ValueError): await module.read_request(reader)

@pytest.mark.asyncio
async def test_frame_empty_eof_is_clean_but_ambiguous_json_denied():
    module = api()
    reader = asyncio.StreamReader(limit=16385)
    reader.feed_eof()
    assert await module.read_request(reader) is None
    for payload in (b'{"a":1,"a":2}\n', b'{"x":NaN}\n', b'[]\n'):
        reader = asyncio.StreamReader(limit=16385)
        reader.feed_data(payload)
        with pytest.raises(ValueError): await module.read_request(reader)
