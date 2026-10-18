"""단발 판독기의 시간·영속 once·입력/출력 경계 회귀 검사."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/ops/run_capture_readout.py'


def runner():
    assert SCRIPT.exists(), 'one-time readout runner has not been implemented'
    spec = importlib.util.spec_from_file_location('capture_readout_runner', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup(tmp_path):
    mod = runner()
    root = tmp_path / 'source'
    (root / 'scripts/ops').mkdir(parents=True)
    (root / 'src').mkdir()
    (root / 'src/__init__.py').write_text('')
    (root / 'scripts/ops/run_capture_readout.py').write_text('# synthetic source\n')
    responses = {
        'report_capture_readiness.py': {'journal': {'verified': True, 'complete': True, 'dropped_records': 0}, 'technical': {'technical_ready': True}, 'decision_recording_finality': 'unverified'},
        'report_entry_gate_trace.py': {'study_binding_validated': True},
        'report_kis_frame_diagnostics.py': {'study_binding_validated': True},
        'prepare_entry_evaluation.py': {'production_eligible': False, 'account_return': None, 'review_tasks': []},
    }
    for name, response in responses.items():
        (root / 'scripts' / name).write_text('import json\nprint(' + repr(json.dumps(response)) + ')\n')
    mod.ROOT = root
    study = tmp_path / 'study.json'
    study.write_text('{}')
    ledger = tmp_path / 'ledger.jsonl'
    ledger.write_text('synthetic ledger\n')
    decisions = tmp_path / 'decisions.jsonl'
    decisions.write_text('{}\n')
    now = datetime(2026, 10, 16, 0, 45, tzinfo=timezone.utc)
    manifest = {
        'version': 'deterministic-capture-readout-v1',
        'study': str(study), 'study_sha256': hashlib.sha256(study.read_bytes()).hexdigest(),
        'ledger': str(ledger), 'decisions': str(decisions),
        'output_dir': str(tmp_path / 'output'),
        'not_before': '2026-10-16T09:42:00+09:00',
        'not_after': '2026-10-16T10:00:00+09:00',
        'source_sha256': mod.source_fingerprint(),
    }
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    return mod, manifest, path, now


def save(setup):
    mod, manifest, path, now = setup
    path.write_text(json.dumps(manifest))
    return mod.run(path, now=lambda: now)


@pytest.mark.parametrize('shift', [-3600, -86400, 86400, 3600])
def test_outside_exact_window_does_not_consume_attempt(setup, shift):
    mod, manifest, path, now = setup
    result = mod.run(path, now=lambda: now + timedelta(seconds=shift))
    assert result['status'] == 'time_rejected'
    assert not Path(manifest['output_dir']).exists()


def test_check_accepts_future_missing_files_without_consuming_once(setup):
    mod, manifest, path, _ = setup
    for key in ('study', 'ledger', 'decisions'):
        Path(manifest[key]).unlink()
    assert mod.check(path)['status'] == 'preflight_ok'
    assert not Path(manifest['output_dir']).exists()


def test_success_is_private_unreviewed_and_duplicate_never_executes(setup):
    mod, manifest, path, now = setup
    original = {key: Path(manifest[key]).read_bytes() for key in ('study', 'ledger', 'decisions')}
    result = save(setup)
    assert result['status'] == 'diagnostics_complete'
    assert result['human_review_required'] is True
    assert result['production_eligible'] is False
    assert result['account_return'] is None
    assert result['decision_recording_finality'] == 'unverified'
    out = Path(manifest['output_dir'])
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert len(result['commands']) == 4
    for file in out.rglob('*'):
        assert stat.S_IMODE(file.stat().st_mode) == (0o700 if file.is_dir() else 0o600)
    before = {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}
    assert mod.run(path, now=lambda: now)['status'] == 'already_attempted'
    assert before == {p.name: p.read_bytes() for p in out.iterdir() if p.is_file()}
    assert original == {key: Path(manifest[key]).read_bytes() for key in original}


def test_existing_incomplete_attempt_is_never_retried(setup):
    mod, manifest, path, now = setup
    out = Path(manifest['output_dir'])
    out.mkdir(mode=0o700)
    (out / 'attempted.json').write_bytes(b'')
    assert mod.run(path, now=lambda: now)['status'] == 'already_attempted'
    assert not (out / 'summary.json').exists()


def test_concurrent_lock_does_not_consume_once(setup):
    mod, manifest, path, now = setup
    out = Path(manifest['output_dir'])
    out.mkdir(mode=0o700)
    fd = os.open(out / 'run.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert mod.run(path, now=lambda: now)['status'] == 'busy'
        assert not (out / 'attempted.json').exists()
    finally:
        os.close(fd)


@pytest.mark.parametrize('kind', ['missing_study', 'missing_ledger', 'study_hash', 'ledger_oversize', 'decisions_oversize'])
def test_bad_inputs_leave_durable_blocked_attempt(setup, kind):
    mod, manifest, path, now = setup
    if kind.startswith('missing_'):
        Path(manifest[kind.removeprefix('missing_')]).unlink()
    elif kind == 'study_hash':
        manifest['study_sha256'] = '0' * 64
    else:
        key = kind.removesuffix('_oversize')
        with open(manifest[key], 'wb') as handle:
            handle.truncate(268435457)
    result = save(setup)
    assert result['status'] == 'blocked'
    assert result['commands'] == []
    assert Path(manifest['output_dir'], 'attempted.json').exists()
    assert mod.run(path, now=lambda: now)['status'] == 'already_attempted'


@pytest.mark.parametrize('field', ['study', 'ledger', 'decisions', 'output_dir'])
def test_symlink_paths_rejected_without_touching_target(setup, field, tmp_path):
    mod, manifest, path, now = setup
    source = Path(manifest[field])
    target = tmp_path / ('target-' + field)
    if field == 'output_dir':
        target.mkdir(mode=0o700)
    else:
        source.rename(target)
    source.symlink_to(target)
    result = save(setup)
    assert result['status'] in ('blocked', 'invalid_manifest')
    assert target.exists()
    if field == 'output_dir':
        assert list(target.iterdir()) == []


def test_source_fingerprint_mismatch_never_runs_child(setup):
    mod, manifest, _, _ = setup
    (mod.ROOT / 'scripts/report_capture_readiness.py').write_text('raise RuntimeError("must not run")\n')
    result = save(setup)
    assert result['status'] == 'invalid_manifest'
    assert not Path(manifest['output_dir']).exists()


def test_failed_subtool_does_not_stop_remaining_diagnostics(setup):
    mod, manifest, _, _ = setup
    (mod.ROOT / 'scripts/report_capture_readiness.py').write_text('raise SystemExit(2)\n')
    manifest['source_sha256'] = mod.source_fingerprint()
    result = save(setup)
    assert result['status'] == 'blocked'
    assert len(result['commands']) == 4
    assert result['commands'][0]['returncode'] == 2
    assert result['commands'][3]['returncode'] == 0


def test_rc_zero_does_not_override_incomplete_journal(setup):
    mod, manifest, _, _ = setup
    (mod.ROOT / 'scripts/report_capture_readiness.py').write_text('print(\'{"journal":{"verified":true,"complete":false,"dropped_records":1},"technical":{"technical_ready":false}}\')\n')
    manifest['source_sha256'] = mod.source_fingerprint()
    result = save(setup)
    assert result['status'] == 'blocked'
    assert all(command['returncode'] == 0 for command in result['commands'])


def test_child_environment_and_working_directory_do_not_expose_parent_secrets(setup, monkeypatch):
    mod, manifest, _, _ = setup
    monkeypatch.setenv('KIS_APP_SECRET', 'test-secret-do-not-pass')
    monkeypatch.setenv('PYTHONPATH', '/invalid')
    script = mod.ROOT / 'scripts/report_entry_gate_trace.py'
    script.write_text('import json,os\nprint(json.dumps({"secret_present":"KIS_APP_SECRET" in os.environ,"cwd":os.getcwd(),"home":os.environ["HOME"]}))\n')
    manifest['source_sha256'] = mod.source_fingerprint()
    save(setup)
    report = json.loads(Path(manifest['output_dir'], 'gate-trace.json').read_text())
    assert report['secret_present'] is False
    assert report['cwd'] == manifest['output_dir']
    assert report['home'] == str(Path(manifest['output_dir'], 'home'))


def test_cli_has_no_backdated_clock_flag():
    runner()
    result = subprocess.run([sys.executable, str(SCRIPT), '--now', '2026-10-16T09:45:00+09:00'], capture_output=True, text=True)
    assert result.returncode != 0


@pytest.mark.parametrize('defect', ['extra_command', 'naive_time', 'multi_day', 'duplicate_key', 'relative_path'])
def test_ambiguous_or_extensible_manifest_is_rejected(setup, defect):
    mod, manifest, path, now = setup
    if defect == 'extra_command':
        manifest['command'] = '/bin/true'
    elif defect == 'naive_time':
        manifest['not_before'] = '2026-10-16T09:42:00'
    elif defect == 'multi_day':
        manifest['not_after'] = '2026-10-17T10:00:00+09:00'
    elif defect == 'relative_path':
        manifest['ledger'] = 'ledger.jsonl'
    if defect == 'duplicate_key':
        path.write_text(json.dumps(manifest)[:-1] + ',"version":"deterministic-capture-readout-v1"}')
        result = mod.run(path, now=lambda: now)
    else:
        result = save(setup)
    assert result['status'] == 'invalid_manifest'
    assert not Path(manifest['output_dir']).exists()


@pytest.mark.parametrize('field', ['study', 'output_dir'])
def test_preflight_rejects_existing_symlink_paths(setup, field, tmp_path):
    mod, manifest, path, _ = setup
    original = Path(manifest[field])
    target = tmp_path / 'target'
    if original.exists():
        original.rename(target)
    else:
        target.mkdir(mode=0o700)
    original.symlink_to(target)
    assert mod.check(path)['status'] == 'invalid_manifest'


def test_preflight_rejects_existing_wrong_study_hash(setup):
    mod, manifest, path, _ = setup
    Path(manifest['study']).write_text('{"changed":true}')
    assert mod.check(path)['status'] == 'invalid_manifest'
    assert not Path(manifest['output_dir']).exists()


def test_preflight_missing_nested_future_inputs_is_allowed(setup, tmp_path):
    mod, manifest, path, _ = setup
    manifest['ledger'] = str(tmp_path / 'future' / 'not-created' / 'ledger.jsonl')
    path.write_text(json.dumps(manifest))
    assert mod.check(path)['status'] == 'preflight_ok'


def test_decision_snapshot_uses_fixed_prefix_when_writer_appends(setup, monkeypatch):
    mod, manifest, _, _ = setup
    original = mod._new_file
    def append_on_copy(fd, name):
        if name == 'decisions.jsonl':
            with open(manifest['decisions'], 'ab') as writer:
                writer.write(b'{"new":true}\n')
        return original(fd, name)
    monkeypatch.setattr(mod, '_new_file', append_on_copy)
    result = save(setup)
    assert Path(manifest['output_dir'], 'decisions.jsonl').read_bytes() == b'{}\n'
    assert result['inputs']['decisions']['bytes'] == 3
    assert result['decision_recording_finality'] == 'unverified'
    assert Path(manifest['decisions']).read_bytes() == b'{}\n{"new":true}\n'


def test_killed_attempt_is_durable_and_never_restarted(setup):
    import signal
    mod, manifest, path, now = setup
    child = os.fork()
    if child == 0:
        mod._execute = lambda *args: os.kill(os.getpid(), signal.SIGKILL)
        mod.run(path, now=lambda: now)
        os._exit(98)
    _, status = os.waitpid(child, 0)
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    assert json.loads(Path(manifest['output_dir'], 'attempted.json').read_text())['state'] == 'attempted_no_automatic_retry'
    assert not Path(manifest['output_dir'], 'summary.json').exists()
    assert mod.run(path, now=lambda: now)['status'] == 'already_attempted'


@pytest.mark.parametrize('fault', ['timeout', 'output_limit', 'non_json'])
def test_child_resource_or_protocol_failure_is_bounded_and_blocked(setup, monkeypatch, fault):
    mod, manifest, _, _ = setup
    if fault == 'timeout':
        monkeypatch.setattr(mod, 'CHILD_TIMEOUT', 0.1)
        code = 'import time\ntime.sleep(5)\n'
    elif fault == 'output_limit':
        monkeypatch.setattr(mod, 'MAX_OUTPUT', 4096)
        code = 'import os\nwhile True: os.write(1,b"x"*8192)\n'
    else:
        code = 'print("not JSON")\n'
    (mod.ROOT / 'scripts/report_capture_readiness.py').write_text(code)
    manifest['source_sha256'] = mod.source_fingerprint()
    result = save(setup)
    assert result['status'] == 'blocked'
    assert len(result['commands']) == 4
    assert result['commands'][0]['valid_json'] is False
    if fault == 'timeout':
        assert result['commands'][0]['timed_out'] is True
    if fault == 'output_limit':
        assert Path(manifest['output_dir'], 'readiness.json').stat().st_size <= 4096


def test_symlink_in_input_ancestor_is_rejected(setup, tmp_path):
    mod, manifest, _, _ = setup
    (tmp_path / 'alias').symlink_to(tmp_path, target_is_directory=True)
    manifest['ledger'] = str(tmp_path / 'alias/ledger.jsonl')
    result = save(setup)
    assert result['status'] == 'blocked'
    assert result['commands'] == []


def test_fifo_input_is_rejected_without_blocking(setup, tmp_path):
    _, manifest, _, _ = setup
    Path(manifest['ledger']).unlink()
    os.mkfifo(manifest['ledger'])
    result = save(setup)
    assert result['status'] == 'blocked'
    assert result['commands'] == []


@pytest.mark.parametrize('unsealed', [False, True])
def test_real_four_clis_with_synthetic_capture(tmp_path, unsealed):
    from test_capture_readiness import _capture
    mod = runner()
    study, ledger, _, end = _capture(tmp_path, empty=True)
    ledger = Path(ledger)
    if unsealed:
        ledger.write_bytes(b''.join(ledger.read_bytes().splitlines(keepends=True)[:-1]))
    decisions = tmp_path / 'decisions.jsonl'
    decisions.write_text('')
    now = end + timedelta(minutes=2)
    manifest = {
        'version': 'deterministic-capture-readout-v1',
        'study': str(study), 'study_sha256': hashlib.sha256(Path(study).read_bytes()).hexdigest(),
        'ledger': str(ledger), 'decisions': str(decisions), 'output_dir': str(tmp_path / 'result'),
        'not_before': (end + timedelta(minutes=1)).isoformat(),
        'not_after': (end + timedelta(minutes=20)).isoformat(), 'source_sha256': mod.source_fingerprint(),
    }
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    result = mod.run(path, now=lambda: now)
    assert len(result['commands']) == 4
    assert result['status'] == ('blocked' if unsealed else 'diagnostics_complete'), result
    assert result['production_eligible'] is False
    if not unsealed:
        readiness = json.loads(Path(manifest['output_dir'], 'readiness.json').read_text())
        assert readiness['status'] == 'no_llm_checks'
        assert readiness['journal']['complete'] is True
        bundle = json.loads(Path(manifest['output_dir'], 'evaluation-unreviewed.json').read_text())
        assert bundle['production_eligible'] is False
        assert bundle['account_return'] is None


def test_changed_code_cannot_hide_in_legacy_bytecode_cache(setup):
    import py_compile
    mod, manifest, _, _ = setup
    # 고정 소스의 출력과 다른 bytecode를 같은 timestamp/size로 준비한다.
    target = mod.ROOT / 'src/payload.py'
    target.write_text('value = "bad"\n')
    stamp = target.stat().st_mtime
    py_compile.compile(str(target), doraise=True)
    target.write_text('value = "ok!"\n')
    os.utime(target, (stamp, stamp))
    script = mod.ROOT / 'scripts/report_entry_gate_trace.py'
    script.write_text('import sys,json\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parents[1]))\nfrom src.payload import value\nprint(json.dumps({"value":value}))\n')
    manifest['source_sha256'] = mod.source_fingerprint()
    save(setup)
    report = json.loads(Path(manifest['output_dir'], 'gate-trace.json').read_text())
    assert report['value'] == 'ok!'


def test_existing_output_symlink_is_not_overwritten(setup, tmp_path):
    mod, manifest, _, _ = setup
    out = Path(manifest['output_dir'])
    out.mkdir(mode=0o700)
    victim = tmp_path / 'untouched.txt'
    victim.write_text('preserve me')
    (out / 'readiness.json').symlink_to(victim)
    result = save(setup)
    assert result['status'] == 'blocked'
    assert victim.read_text() == 'preserve me'
    assert (out / 'readiness.json').is_symlink()


def test_missing_decision_file_is_not_created_and_finality_stays_unverified(setup):
    _, manifest, _, _ = setup
    Path(manifest['decisions']).unlink()
    result = save(setup)
    assert not Path(manifest['decisions']).exists()
    assert not Path(manifest['output_dir'], 'decisions.jsonl').exists()
    assert result['inputs']['decisions']['status'] == 'missing'
    assert result['decision_recording_finality'] == 'unverified'


def test_manifest_snapshot_is_retained_with_attempt(setup):
    _, manifest, _, _ = setup
    save(setup)
    assert json.loads(Path(manifest['output_dir'], 'manifest.json').read_text()) == manifest


def test_malformed_readiness_shape_is_blocked_with_summary(setup):
    mod, manifest, _, _ = setup
    (mod.ROOT / 'scripts/report_capture_readiness.py').write_text('print(\'{"journal":[],"technical":null}\')\n')
    manifest['source_sha256'] = mod.source_fingerprint()
    result = save(setup)
    assert result['status'] == 'blocked'
    assert json.loads(Path(manifest['output_dir'], 'summary.json').read_text())['status'] == 'blocked'
