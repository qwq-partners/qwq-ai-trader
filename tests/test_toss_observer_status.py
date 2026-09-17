import json
import os

import pytest


def test_status_is_private_atomic_and_fsynced(tmp_path, monkeypatch):
    from src.observation.toss_status import StatusWriter
    calls = []
    original = os.fsync
    monkeypatch.setattr(os, 'fsync', lambda fd: (calls.append(fd), original(fd))[-1])
    writer = StatusWriter(tmp_path / 'status.json')
    writer.write({'state': 'idle', 'production_eligible': False})
    assert json.loads((tmp_path / 'status.json').read_text())['state'] == 'idle'
    assert (tmp_path / 'status.json').stat().st_mode & 0o777 == 0o600
    assert len(calls) >= 2
    writer.write({'state': 'closed'})
    assert json.loads((tmp_path / 'status.json').read_text()) == {'state': 'closed'}


def test_status_refuses_symlink_hardlink_and_oversize(tmp_path):
    from src.observation.toss_status import StatusError, StatusWriter
    outside = tmp_path / 'outside'
    outside.write_text('protected')
    path = tmp_path / 'status.json'
    path.symlink_to(outside)
    with pytest.raises(StatusError):
        StatusWriter(path).write({'state': 'idle'})
    assert outside.read_text() == 'protected'
    path.unlink()
    os.link(outside, path)
    with pytest.raises(StatusError):
        StatusWriter(path).write({'state': 'idle'})
    path.unlink()
    with pytest.raises(StatusError):
        StatusWriter(path).write({'oversize': 'x' * 16384})
    assert not path.exists()


def test_status_fsync_failure_does_not_report_success(tmp_path, monkeypatch):
    from src.observation.toss_status import StatusError, StatusWriter
    monkeypatch.setattr(os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError()))
    with pytest.raises(StatusError):
        StatusWriter(tmp_path / 'status.json').write({'state': 'idle'})
