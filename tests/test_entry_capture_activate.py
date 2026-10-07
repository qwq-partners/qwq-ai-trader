"""실제 프로세스/서비스/API 없이 한 번 활성화 경계를 검증한다."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts/ops/entry_capture_activate.py'
SIGNAL_PROFILE = '20261008-signal-window-v1'
PROFILES = ['20261002', '20261006-pilot2', '20261007-pilot3', SIGNAL_PROFILE]


def module():
    assert SCRIPT.is_file(), 'bounded activation driver is missing'
    spec = importlib.util.spec_from_file_location('entry_capture_activate', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=PROFILES)
def setup(tmp_path, request):
    m = module()
    profile = m.activation_profile(request.param)
    now = profile.not_before
    repo = tmp_path / 'repo'
    repo.mkdir()
    protected = {}
    for name in ('config/default.yml', 'config/evolved_overrides.yml'):
        p = repo / name
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b'preserved\n')
        protected[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    source = repo / 'scripts/run_trader.py'
    source.parent.mkdir()
    source.write_bytes(b'old\n')
    once = tmp_path / request.param / 'once.json'
    once.parent.mkdir()
    once.write_bytes(b'{}')
    launcher, deployment = tmp_path / 'launcher.py', tmp_path / 'deployment.json'
    launcher.write_bytes(b'launcher')
    deployment.write_bytes(b'{}')
    kill = tmp_path / 'KILL_SWITCH_KR'
    kill.touch()
    state = tmp_path / ('state-' + request.param)
    state.mkdir(mode=0o700)
    dropin = tmp_path / 'dropins/entry-capture.conf'
    dropin.parent.mkdir(mode=0o755)
    staged = tmp_path / 'staged.conf'
    staged.write_text(m.dropin_content(str(once)))
    lock = tmp_path / 'deploy.lock'
    lock.touch()
    for p in (lock, once, launcher, deployment, staged): p.chmod(0o600)
    cfg = m.Config(repo=repo, old_head='a'*40, new_head='b'*40,
        source_hashes={'scripts/run_trader.py': hashlib.sha256(b'new\n').hexdigest()},
        protected_hashes=protected,
        input_hashes={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (once, launcher, deployment)},
        once_path=once, launcher_path=launcher, deployment_path=deployment,
        staged_dropin=staged, dropin_path=dropin,
        dropin_sha256=hashlib.sha256(staged.read_bytes()).hexdigest(),
        kill_path=kill, state_dir=state, lock_path=lock,
        not_before=now, latest_start_at=now+timedelta(seconds=120),
        toss_uid=997, toss_gid=987, study_sha256='c'*64,
        evaluation_epoch=profile.evaluation_epoch, engine_only=profile.engine_only)
    events = []
    current = [now]
    health = {'broker': {'connected': True, 'pending_orders': 0},
              'risk_manager': {'pending_orders': 0, 'pending_quantities': 0, 'pending_sells': 0}}
    anchors = {'schema_version':'entry-anchor-projection-v2', 'study_sha256':'c'*64,
               'evaluation_epoch':cfg.evaluation_epoch, 'capture_id':'capture-1',
               'capture_closed':False, 'source_record_count':0, 'records':[]}
    if profile.engine_only:
        cfg.input_hashes.clear()
        for path in (once, once.with_name('study.json'), once.with_name('manifest.json')):
            path.write_bytes(b'{}')
            path.chmod(0o600)
            cfg.input_hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        anchors.update(schema_version='entry-observation-readiness-v1', scan_scope='window')
    behavior = {}

    def runner(argv, *, timeout):
        events.append(tuple(argv))
        assert (state / 'activation.receipt').exists()
        if behavior.get('hook'):
            behavior['hook'](argv)
        if behavior.get('fail') and behavior['fail'](argv):
            raise RuntimeError('secret stdout must never appear')
        out, code = '', 0
        if 'rev-parse' in argv:
            out = cfg.new_head if source.read_bytes() == b'new\n' else cfg.old_head
        elif 'status' in argv:
            out = behavior.get('dirty', ' M config/evolved_overrides.yml\n')
        elif 'show' in argv and '/usr/bin/git' in argv:
            out = b'new\n'
        elif 'checkout' in argv:
            source.write_bytes(b'new\n' if argv[-1] == cfg.new_head else b'old\n')
        elif 'start' in argv:
            behavior['started'] = True
        elif 'is-active' in argv:
            out = 'inactive\n' if argv[-1] == m.TOSS_UNIT and not behavior.get('started') else 'active\n'
            code = 3 if out.startswith('inactive') else 0
        return SimpleNamespace(returncode=code, stdout=out)

    def fetch(path, *, timeout):
        events.append(('http', path))
        if path in ('/api/internal/entry-anchors', '/api/internal/entry-observation-readiness'):
            behavior['anchor_attempts'] = behavior.get('anchor_attempts',0)+1
            if behavior['anchor_attempts'] <= behavior.get('ready_after',0):
                raise ValueError('still starting')
        if behavior.get('fetch_fail') and path in ('/api/internal/entry-anchors', '/api/internal/entry-observation-readiness'):
            raise ValueError('secret account output')
        return health if path == '/api/health' else anchors

    def run():
        return m.activate(cfg, runner=runner, clock=lambda:current[0], fetch=fetch,
                          sleep=lambda seconds:current.__setitem__(0,current[0]+timedelta(seconds=seconds)), trusted_uid=os.getuid(), lock_uid=os.getuid(), engine_input_uid=os.getuid())
    return SimpleNamespace(**locals())


@pytest.mark.parametrize('offset', [-1, 120, 121])
def test_outside_window_has_no_receipt_or_commands(setup, offset):
    s = setup
    s.current[0] = s.now + timedelta(seconds=offset)
    assert s.run()['status'] == 'rejected'
    assert not s.events
    assert not (s.state / 'activation.receipt').exists()


@pytest.mark.parametrize('offset', [0, 119])
def test_success_orders_check_checkout_reload_restart_health_then_toss(setup, offset):
    s = setup
    s.current[0] += timedelta(seconds=offset)
    result = s.run()
    assert result['status'] == 'complete', (result, s.events)
    assert s.source.read_bytes() == b'new\n'
    assert s.dropin.read_bytes() == s.staged.read_bytes()
    commands = s.events
    if s.cfg.engine_only:
        restart = next(i for i, c in enumerate(commands) if 'restart' in c)
        readiness = commands.index(('http', '/api/internal/entry-observation-readiness'))
        assert restart < readiness
        assert not any('--check' in c or 'start' in c for c in commands)
        assert result['toss_attempted'] is False
        assert sum('restart' in c for c in commands) == 1
        count = len(commands)
        assert s.run()['status'] == 'rejected'
        assert len(commands) == count
        return
    check = next(i for i, c in enumerate(commands) if '--check' in c)
    checkout = next(i for i,c in enumerate(commands) if 'checkout' in c)
    restart = next(i for i,c in enumerate(commands) if 'restart' in c)
    start = next(i for i,c in enumerate(commands) if 'start' in c)
    anchor = commands.index(('http','/api/internal/entry-anchors'))
    assert check < checkout < restart < anchor < start
    assert 'TOSS_API=1' in commands[check] and '-I' in commands[check] and '-S' in commands[check]
    assert '--reuid=997' in commands[check] and '--regid=987' in commands[check]
    assert '--groups=987' in commands[check] and '--clear-groups' not in commands[check]
    assert sum('restart' in c for c in commands) == 1
    assert '--no-block' in commands[restart]
    assert sum('start' in c for c in commands) == 1
    count = len(commands)
    assert s.run()['status'] == 'rejected'
    assert len(commands) == count
    assert json.loads((s.state/'status.json').read_text())['status'] == 'complete'


@pytest.mark.parametrize('problem', ['hash', 'kill', 'pending', 'bad_pending', 'dirty', 'dropin'])
def test_preflight_rejects_without_checkout_or_restart(setup, problem):
    s = setup
    if problem == 'hash': s.once.write_bytes(b'changed')
    if problem == 'kill': s.kill.unlink()
    if problem == 'pending': s.health['broker']['pending_orders'] = 1
    if problem == 'bad_pending': s.health['risk_manager']['pending_orders'] = False
    if problem == 'dirty': s.behavior['dirty'] = ' M scripts/run_trader.py\n'
    if problem == 'dropin': s.dropin.write_bytes(b'other')
    assert s.run()['status'] == 'failed'
    assert not any('checkout' in c or 'restart' in c or 'start' in c for c in s.events)


def test_late_after_checkout_rolls_back_without_restart(setup):
    s = setup
    def hook(argv):
        if 'daemon-reload' in argv:
            s.current[0] = s.now+timedelta(seconds=120)
    s.behavior['hook'] = hook
    assert s.run()['status'] == 'failed'
    assert s.source.read_bytes() == b'old\n'
    assert not s.dropin.exists()
    assert not any('restart' in c or 'start' in c for c in s.events)


def test_restart_failure_consumes_attempt_without_rollback_or_second_restart(setup):
    s = setup
    s.behavior['fail'] = lambda argv:'restart' in argv
    result = s.run()
    assert result['status'] == 'failed'
    assert result['restart_attempted'] is True
    assert s.source.read_bytes() == b'new\n'
    assert s.dropin.exists()
    assert sum('restart' in c for c in s.events) == 1
    assert not any('start' in c for c in s.events)
    assert 'secret' not in (s.state/'status.json').read_text()


@pytest.mark.parametrize('problem', ['unhealthy', 'identity', 'late'])
def test_post_restart_failure_never_starts_toss(setup, problem):
    s = setup
    if problem == 'unhealthy': s.behavior['fetch_fail'] = True
    if problem == 'identity': s.anchors['study_sha256'] = 'd'*64
    if problem == 'late':
        s.behavior['hook'] = lambda argv: s.current.__setitem__(0,s.now+timedelta(seconds=120)) if 'restart' in argv else None
    assert s.run()['status'] == 'failed'
    assert sum('restart' in c for c in s.events) == 1
    assert not any('start' in c for c in s.events)
    assert s.source.read_bytes() == b'new\n'


def test_existing_deploy_lock_excludes_activation(setup):
    import fcntl
    s = setup
    with s.lock.open('r') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert s.run()['status'] == 'failed'
    assert not s.events


def test_corrupt_target_source_aborts_before_checkout(setup):
    s = setup
    s.cfg.source_hashes['scripts/run_trader.py'] = '0'*64
    assert s.run()['status'] == 'failed'
    assert not any('checkout' in c for c in s.events)


def test_launcher_failure_never_changes_live_checkout(setup):
    s = setup
    if s.cfg.engine_only:
        pytest.skip('엔진 전용 프로필에는 토스 실행기 검사가 없다')
    s.behavior['fail'] = lambda argv:'--check' in argv
    assert s.run()['status'] == 'failed'
    assert s.source.read_bytes() == b'old\n'
    assert not s.dropin.exists()


def test_kill_removed_during_checkout_rolls_back(setup):
    s = setup
    def hook(argv):
        if 'checkout' in argv and argv[-1] == s.cfg.new_head:
            s.kill.unlink()
    s.behavior['hook'] = hook
    result = s.run()
    assert result['status'] == 'failed'
    assert s.source.read_bytes() == b'old\n'
    assert not any('restart' in c for c in s.events)


def test_config_loader_rejects_unknown_keys_before_any_command(tmp_path, monkeypatch):
    m = module()
    config = tmp_path / 'activation.json'
    config.write_text('{"command":"not permitted"}')
    config.chmod(0o600)
    monkeypatch.setattr(m, 'CONFIG_PATH', config)
    monkeypatch.setattr(m.os, 'geteuid', lambda:0)
    monkeypatch.setattr(m, 'trusted_dir', lambda *args:None)
    original = m.read_file
    monkeypatch.setattr(m, 'read_file', lambda p, **kw: original(p,owner=os.getuid()))
    with pytest.raises(m.GuardError):
        m.load_config()


def test_group_writable_staged_file_is_rejected(setup):
    s = setup
    s.staged.chmod(0o660)
    assert s.run()['status'] == 'failed'
    assert not s.events


def test_symlink_receipt_does_not_overwrite_other_file(setup):
    s = setup
    victim = s.tmp_path/'victim'
    victim.write_text('preserve')
    (s.state/'activation.receipt').symlink_to(victim)
    assert s.run()['status'] == 'rejected'
    assert victim.read_text() == 'preserve'
    assert not s.events


def test_successful_toss_start_is_not_retried_on_status_failure(setup):
    s = setup
    if s.cfg.engine_only:
        pytest.skip('엔진 전용 프로필에는 토스 시작이 없다')
    def hook(argv):
        if 'start' in argv:
            s.current[0] = s.now+timedelta(seconds=120)
    s.behavior['hook'] = hook
    assert s.run()['status'] == 'failed'
    assert sum('start' in c for c in s.events) == 1
    assert sum('restart' in c for c in s.events) == 1


@pytest.mark.parametrize('problem', [None, 'credentials', 'repo', 'time', 'group', 'uid', 'source', 'study_hash'])
def test_fixed_configuration_contract(setup, monkeypatch, problem):
    s = setup
    m = s.m
    raw = profile_raw(s, s.request.param)
    if problem == 'study_hash': raw['study_sha256'] = 'f'*64
    if problem == 'credentials': raw['input_hashes']['/etc/qwq-toss-observer/credentials.env'] = 'f'*64
    if problem == 'repo': raw['repo'] = '/tmp/other'
    if problem == 'time': raw['latest_start_at'] = raw['latest_start_at'].replace('23:57', '23:58')
    if problem == 'group': raw['toss_gid'] = 0
    if problem == 'uid': raw['toss_uid'] = 0
    if problem == 'source': raw['source_hashes']['.env'] = 'f'*64
    monkeypatch.setattr(m.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(m, 'trusted_dir', lambda *args: None)
    monkeypatch.setattr(m, 'read_file', lambda *args, **kwargs: json.dumps(raw).encode())
    if problem:
        with pytest.raises(m.GuardError): m.load_config(s.request.param)
    else:
        assert m.load_config(s.request.param).toss_gid == 987


def test_slow_healthy_start_waits_beyond_ten_polls(setup):
    s = setup
    s.behavior['ready_after'] = 30
    assert s.run()['status'] == 'complete'
    assert s.behavior['anchor_attempts'] == 31
    assert s.current[0] == s.now+timedelta(seconds=30)
    assert sum('restart' in c for c in s.events) == 1


def test_readiness_wait_stops_exactly_at_deadline_without_toss(setup):
    s = setup
    s.behavior['fetch_fail'] = True
    assert s.run()['status'] == 'failed'
    assert s.current[0] == s.now+timedelta(seconds=120)
    assert not any('start' in c for c in s.events)
    assert sum('restart' in c for c in s.events) == 1


def test_partial_dropin_write_is_removed_by_created_inode_on_rollback(setup, monkeypatch):
    s = setup
    original = s.m.os.fsync
    injected = [False]
    def fsync(fd):
        if s.dropin.exists() and not injected[0]:
            opened,visible = os.fstat(fd),s.dropin.stat()
            if (opened.st_dev,opened.st_ino)==(visible.st_dev,visible.st_ino):
                injected[0]=True
                os.ftruncate(fd,3)
                raise OSError('injected_partial_dropin_write')
        return original(fd)
    monkeypatch.setattr(s.m.os,'fsync',fsync)
    result=s.run()
    assert injected[0]
    assert result['status']=='failed' and result['rollback']=='complete'
    assert not s.dropin.exists()
    assert s.source.read_bytes()==b'old\n'
    assert not any('restart' in c for c in s.events)


def test_rollback_never_removes_replacement_dropin_inode(setup, monkeypatch):
    s=setup
    original=s.m.os.fsync
    injected=[False]
    def fsync(fd):
        if s.dropin.exists() and not injected[0]:
            opened,visible=os.fstat(fd),s.dropin.stat()
            if (opened.st_dev,opened.st_ino)==(visible.st_dev,visible.st_ino):
                injected[0]=True
                s.dropin.rename(s.dropin.with_suffix('.original'))
                s.dropin.write_bytes(b'independent-replacement')
                raise OSError('injected_inode_replacement')
        return original(fd)
    monkeypatch.setattr(s.m.os,'fsync',fsync)
    result=s.run()
    assert result['rollback']=='failed'
    assert s.dropin.read_bytes()==b'independent-replacement'
    assert not any('restart' in c for c in s.events)


def profile_raw(s, profile):
    from dataclasses import asdict
    m = s.m
    raw = asdict(s.cfg)
    raw.pop('engine_only')
    if profile == '20261002':
        once = Path('/home/ubuntu/.local/share/qwq-entry-observation/20261002-pilot1/once.json')
        staged = Path('/etc/qwq-entry-capture/entry-capture.conf')
        state = Path('/var/lib/qwq-entry-capture')
        date, epoch = '2026-10-01', 'kr-entry-20261002-firstscan-v1'
    elif profile == '20261006-pilot2':
        once = Path('/home/ubuntu/.local/share/qwq-entry-observation/20261006-pilot2/once.json')
        staged = Path('/etc/qwq-entry-capture/20261006-pilot2/entry-capture.conf')
        state = Path('/var/lib/qwq-entry-capture/20261006-pilot2')
        date, epoch = '2026-10-05', 'kr-entry-20261006-firstscan-v4'
    elif profile == SIGNAL_PROFILE:
        once = Path('/home/ubuntu/.local/share/qwq-entry-observation/20261008-signal-window-v1/once.json')
        staged = Path('/etc/qwq-entry-capture/20261008-signal-window-v1-entry-capture.conf')
        state = Path('/var/lib/qwq-entry-capture/20261008-signal-window-v1')
        date, epoch = '2026-10-07', '2026-10-08-engine-entry-signal-window-v1'
    else:
        assert profile == '20261007-pilot3'
        once = Path('/home/ubuntu/.local/share/qwq-entry-observation/20261007-pilot3/once.json')
        staged = Path('/etc/qwq-entry-capture/20261007-pilot3/entry-capture.conf')
        state = Path('/var/lib/qwq-entry-capture/20261007-pilot3')
        date, epoch = '2026-10-06', 'kr-entry-20261007-firstscan-v4'
    fixed = dict(repo=m.REPO, once_path=once, launcher_path=m.LAUNCHER_PATH,
        deployment_path=m.DEPLOYMENT_PATH, staged_dropin=staged,
        dropin_path=m.DROPIN_PATH, kill_path=m.KILL_PATH, state_dir=state, lock_path=m.LOCK_PATH)
    raw.update({k:str(v) for k,v in fixed.items()})
    raw.update(not_before=date+'T23:55:00+00:00', latest_start_at=date+'T23:57:00+00:00',
               evaluation_epoch=epoch, study_sha256='e'*64)
    raw['input_hashes'] = {str(p):'e'*64 for p in (
        once, once.with_name('study.json'), once.with_name('manifest.json'),
        m.LAUNCHER_PATH,m.DEPLOYMENT_PATH,Path('/etc/qwq-toss-observer/plan.json'),Path('/etc/qwq-toss-observer/registry.json'))}
    if profile == SIGNAL_PROFILE:
        raw['input_hashes'] = {str(p): 'e'*64 for p in (once, once.with_name('study.json'), once.with_name('manifest.json'))}
    return raw


def test_equal_heads_permitted_only_by_predeployed_profiles(setup, monkeypatch):
    s = setup
    m = s.m
    profile = s.request.param
    raw = profile_raw(s, profile)
    raw['old_head'] = raw['new_head']
    monkeypatch.setattr(m.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(m, 'trusted_dir', lambda *args: None)
    monkeypatch.setattr(m, 'read_file', lambda *args, **kwargs: json.dumps(raw).encode())
    if profile == '20261002':
        with pytest.raises(m.GuardError):
            m.load_config(profile)
    else:
        cfg = m.load_config(profile)
        assert cfg.old_head == cfg.new_head


def predeployed_run(s, *, runner=None):
    from dataclasses import replace
    return s.m.activate(replace(s.cfg, old_head=s.cfg.new_head),
        runner=runner or s.runner, clock=lambda: s.current[0], fetch=s.fetch,
        sleep=lambda seconds: s.current.__setitem__(0, s.current[0]+timedelta(seconds=seconds)),
        trusted_uid=os.getuid(), lock_uid=os.getuid(), engine_input_uid=os.getuid())


def test_predeployed_capture_restarts_once_without_checkout(setup):
    s = setup
    s.source.write_bytes(b'new\n')
    result = predeployed_run(s)
    assert result['status'] == 'complete'
    assert not any('checkout' in c for c in s.events)
    assert sum('restart' in c for c in s.events) == 1
    assert sum('start' in c for c in s.events) == (0 if s.cfg.engine_only else 1)
    assert s.source.read_bytes() == b'new\n'
    assert s.dropin.read_bytes() == s.staged.read_bytes()
    count = len(s.events)
    assert predeployed_run(s)['status'] == 'rejected'
    assert len(s.events) == count


@pytest.mark.parametrize('problem', ['head', 'source', 'pending', 'kill', 'input'])
def test_predeployed_checks_current_code_and_all_preflight_gates(setup, problem):
    s = setup
    s.source.write_bytes(b'new\n')
    if problem in ('head', 'source'):
        s.source.write_bytes(b'tampered\n')
    if problem == 'pending':
        s.health['broker']['pending_orders'] = 1
    if problem == 'kill':
        s.kill.unlink()
    if problem == 'input':
        s.once.write_bytes(b'tampered')
    def runner(argv, **kwargs):
        result = s.runner(argv, **kwargs)
        if problem == 'source' and 'rev-parse' in argv:
            result.stdout = s.cfg.new_head
        return result
    assert predeployed_run(s, runner=runner)['status'] == 'failed'
    assert not any('checkout' in c or 'restart' in c or 'start' in c for c in s.events)
    assert not s.dropin.exists()


def test_predeployed_expiry_removes_own_dropin_without_checkout(setup):
    s = setup
    s.source.write_bytes(b'new\n')
    def hook(argv):
        if 'daemon-reload' in argv:
            s.current[0] = s.now+timedelta(seconds=120)
    s.behavior['hook'] = hook
    result = predeployed_run(s)
    assert result['status'] == 'failed' and result['rollback'] == 'complete'
    assert not s.dropin.exists()
    assert s.source.read_bytes() == b'new\n'
    assert not any('checkout' in c or 'restart' in c or 'start' in c for c in s.events)


@pytest.mark.parametrize('replacement', [False, True])
def test_predeployed_partial_dropin_cleanup_respects_inode(setup, monkeypatch, replacement):
    s = setup
    s.source.write_bytes(b'new\n')
    original = s.m.os.fsync
    injected = [False]
    def fsync(fd):
        if s.dropin.exists() and not injected[0]:
            opened, visible = os.fstat(fd), s.dropin.stat()
            if (opened.st_dev, opened.st_ino) == (visible.st_dev, visible.st_ino):
                injected[0] = True
                if replacement:
                    s.dropin.rename(s.dropin.with_suffix('.original'))
                    s.dropin.write_bytes(b'independent-replacement')
                else:
                    os.ftruncate(fd, 3)
                raise OSError('injected dropin failure')
        return original(fd)
    monkeypatch.setattr(s.m.os, 'fsync', fsync)
    result = predeployed_run(s)
    assert injected[0] and result['status'] == 'failed'
    assert result['rollback'] == ('failed' if replacement else 'complete')
    if replacement:
        assert s.dropin.read_bytes() == b'independent-replacement'
    else:
        assert not s.dropin.exists()
    assert s.source.read_bytes() == b'new\n'
    assert not any('checkout' in c or 'restart' in c for c in s.events)


def test_predeployed_restart_failure_never_rolls_back_or_retries(setup):
    s = setup
    s.source.write_bytes(b'new\n')
    s.behavior['fail'] = lambda argv: 'restart' in argv
    result = predeployed_run(s)
    assert result['status'] == 'failed' and result['restart_attempted'] is True
    assert result['rollback'] == 'not_needed'
    assert s.dropin.exists() and s.source.read_bytes() == b'new\n'
    assert sum('restart' in c for c in s.events) == 1
    assert not any('checkout' in c or 'start' in c for c in s.events)


@pytest.mark.parametrize('other_offset', [1, 2, 3])
@pytest.mark.parametrize('mismatch', [None, 'once_path', 'staged_dropin', 'state_dir',
    'evaluation_epoch', 'not_before', 'latest_start_at', 'input_hashes', 'study_sha256'])
def test_profile_binds_every_repeated_identity_and_uses_separate_config(setup, monkeypatch, mismatch, other_offset):
    s = setup
    m = s.m
    profile = s.request.param
    other_profile = PROFILES[(PROFILES.index(profile) + other_offset) % len(PROFILES)]
    raw = profile_raw(s, profile)
    expected = dict(raw)
    other = profile_raw(s, other_profile)
    if mismatch == 'study_sha256':
        raw[mismatch] = 'f'*64
    elif mismatch:
        raw[mismatch] = other[mismatch]
    config_paths = {
        '20261002': Path('/etc/qwq-entry-capture/activation.json'),
        '20261006-pilot2': Path('/etc/qwq-entry-capture/20261006-pilot2/activation.json'),
        '20261007-pilot3': Path('/etc/qwq-entry-capture/20261007-pilot3/activation.json'),
        SIGNAL_PROFILE: Path('/etc/qwq-entry-capture/20261008-signal-window-v1.json'),
    }
    reads = []
    def read(path, **kwargs):
        reads.append(path)
        assert path == config_paths[profile]
        assert kwargs['owner'] == 0
        return json.dumps(raw).encode()
    monkeypatch.setattr(m.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(m, 'trusted_dir', lambda *args: None)
    monkeypatch.setattr(m, 'read_file', read)
    if mismatch:
        with pytest.raises(m.GuardError): m.load_config(profile)
    else:
        cfg = m.load_config(profile)
        for key in ('once_path', 'staged_dropin', 'state_dir'):
            assert str(getattr(cfg, key)) == expected[key]
        for key in ('not_before', 'latest_start_at'):
            assert getattr(cfg, key) == datetime.fromisoformat(expected[key])
        assert cfg.evaluation_epoch == expected['evaluation_epoch']
        assert str(cfg.once_path) in m.dropin_content(str(cfg.once_path))
    assert reads == [config_paths[profile]]


@pytest.mark.parametrize('args', [
    ['--profile', 'arbitrary'], ['--profile', '20261002'],
    ['--profile', '20261006-pilot2', 'extra'], ['--profile', '20261007-pilot3', 'extra'],
    ['--profile', '20261007'], ['--profile', '20261008-pilot4'],
    ['--date', '20261007'], ['/tmp/config'],
    ['--profile', '/etc/qwq-entry-capture/20261007-pilot3/activation.json'],
])
def test_cli_rejects_unknown_profile_before_configuration_or_commands(monkeypatch, args, capsys):
    m = module()
    def forbidden(*args, **kwargs): pytest.fail('invalid selector reached configuration or activation')
    monkeypatch.setattr(m, 'load_config', forbidden)
    monkeypatch.setattr(m, 'activate', forbidden)
    monkeypatch.setattr(m.sys, 'argv', ['driver', *args])
    assert m.main() == 1
    assert json.loads(capsys.readouterr().out)['phase'] == 'configuration'


@pytest.mark.parametrize('args, profile', [
    ([], '20261002'),
    (['--profile', '20261006-pilot2'], '20261006-pilot2'),
    (['--profile', '20261007-pilot3'], '20261007-pilot3'),
    (['--profile', SIGNAL_PROFILE], SIGNAL_PROFILE),
])
def test_cli_passes_only_the_frozen_profile(monkeypatch, capsys, args, profile):
    m = module()
    selected = []
    cfg = object()
    def load(profile='20261002'):
        selected.append(profile)
        return cfg
    def activate(actual):
        assert actual is cfg
        return {'status': 'complete'}
    monkeypatch.setattr(m, 'load_config', load)
    monkeypatch.setattr(m, 'activate', activate)
    monkeypatch.setattr(m.sys, 'argv', ['driver', *args])
    assert m.main() == 0
    assert selected == [profile]
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'


def test_engine_input_owner_uses_selected_once_directory(setup,monkeypatch):
    s=setup; original=s.m.read_file; calls=[]
    def read(path,**kwargs):
        calls.append((path,kwargs.get('owner')))
        if path==s.once:
            assert kwargs.get('owner')==os.getuid()
            raise s.m.GuardError()
        return original(path,**kwargs)
    monkeypatch.setattr(s.m,'read_file',read)
    result=s.run()
    assert result['status']=='failed' and result['phase']=='preflight'
    assert (s.once,os.getuid()) in calls
    assert not any('restart' in c or 'checkout' in c for c in s.events)


@pytest.mark.parametrize('predeployed', [False, True])
def test_other_profile_receipt_does_not_consume_selected_state(setup, monkeypatch, predeployed):
    s = setup
    preserved = {}
    for profile in PROFILES:
        if profile == s.request.param:
            continue
        old_state = s.tmp_path / ('state-' + profile)
        old_state.mkdir()
        for name, value in [('activation.receipt', b'old consumed attempt'), ('status.json', b'old result')]:
            path = old_state / name
            path.write_bytes(value)
            preserved[path] = value
    monkeypatch.setattr(s.m, 'STATE_DIR', old_state)
    if predeployed:
        s.source.write_bytes(b'new\n')
    run = (lambda: predeployed_run(s)) if predeployed else s.run
    assert run()['status'] == 'complete'
    assert all(path.read_bytes() == value for path, value in preserved.items())
    receipt = json.loads((s.state / 'activation.receipt').read_text())
    assert receipt['new_head'] == s.cfg.new_head
    assert datetime.fromisoformat(receipt['attempted_at']) == s.profile.not_before
    assert json.loads((s.state / 'status.json').read_text())['status'] == 'complete'
    assert str(s.once) in s.dropin.read_text()
    count = len(s.events)
    assert run()['status'] == 'rejected'
    assert len(s.events) == count
    assert all(path.read_bytes() == value for path, value in preserved.items())


@pytest.mark.parametrize('setup', [SIGNAL_PROFILE], indirect=True)
def test_signal_window_never_reads_toss_files_or_starts_toss(setup):
    s = setup
    s.launcher.unlink()
    s.deployment.unlink()
    s.behavior['fail'] = lambda argv: '--check' in argv or 'start' in argv
    s.source.write_bytes(b'new\n')
    result = predeployed_run(s)
    assert result['status'] == 'complete'
    assert result['toss_attempted'] is False
    assert not any('checkout' in c or '--check' in c or 'start' in c for c in s.events)
    assert ('http', '/api/internal/entry-anchors') not in s.events
    restart = next(i for i, c in enumerate(s.events) if 'restart' in c)
    toss_checks = [i for i, c in enumerate(s.events) if 'is-active' in c and s.m.TOSS_UNIT in c]
    assert any(i < restart for i in toss_checks)
    assert any(i > restart for i in toss_checks)


@pytest.mark.parametrize('setup', [SIGNAL_PROFILE], indirect=True)
@pytest.mark.parametrize('problem', ['schema_version', 'study_sha256', 'evaluation_epoch',
    'capture_id', 'capture_closed', 'scan_scope', 'unavailable'])
def test_signal_readiness_rejects_wrong_or_closed_runtime_without_retry(setup, problem):
    s = setup
    if problem == 'unavailable':
        s.behavior['fetch_fail'] = True
    else:
        s.anchors[problem] = {
            'schema_version': 'entry-anchor-projection-v2', 'study_sha256': 'f'*64,
            'evaluation_epoch': 'old', 'capture_id': '', 'capture_closed': True,
            'scan_scope': 'first_scan',
        }[problem]
    result = s.run()
    assert result['status'] == 'failed' and result['phase'] == 'readiness'
    assert result['restart_attempted'] is True and result['toss_attempted'] is False
    assert result['rollback'] == 'not_needed'
    assert sum('restart' in c for c in s.events) == 1
    assert not any('start' in c for c in s.events)
    count = len(s.events)
    assert s.run()['status'] == 'rejected'
    assert len(s.events) == count


@pytest.mark.parametrize('setup', [SIGNAL_PROFILE], indirect=True)
@pytest.mark.parametrize('when', ['before', 'after'])
def test_signal_window_fails_when_toss_is_active(setup, when):
    s = setup
    if when == 'before':
        s.behavior['started'] = True
    else:
        def hook(argv):
            if 'restart' in argv:
                s.behavior['started'] = True
        s.behavior['hook'] = hook
    result = s.run()
    assert result['status'] == 'failed'
    assert result['restart_attempted'] is (when == 'after')
    assert sum('restart' in c for c in s.events) == (1 if when == 'after' else 0)
    assert not any('start' in c for c in s.events)


@pytest.mark.parametrize('setup', [SIGNAL_PROFILE], indirect=True)
@pytest.mark.parametrize('problem', [None, 'editable_engine_flag', 'toss_input', 'missing_manifest'])
def test_signal_configuration_derives_engine_only_and_exact_inputs(setup, monkeypatch, problem):
    s = setup
    raw = profile_raw(s, SIGNAL_PROFILE)
    raw['old_head'] = raw['new_head']
    if problem == 'editable_engine_flag': raw['engine_only'] = False
    if problem == 'toss_input': raw['input_hashes'][str(s.m.LAUNCHER_PATH)] = 'e'*64
    if problem == 'missing_manifest':
        del raw['input_hashes'][str(Path(raw['once_path']).with_name('manifest.json'))]
    monkeypatch.setattr(s.m.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(s.m, 'trusted_dir', lambda *args: None)
    monkeypatch.setattr(s.m, 'read_file', lambda *args, **kwargs: json.dumps(raw).encode())
    if problem:
        with pytest.raises(s.m.GuardError): s.m.load_config(SIGNAL_PROFILE)
    else:
        cfg = s.m.load_config(SIGNAL_PROFILE)
        assert cfg.engine_only is True and cfg.old_head == cfg.new_head


def test_signal_profile_is_available_with_predeployed_engine_only_contract():
    m = module()
    try:
        profile = m.activation_profile(SIGNAL_PROFILE)
    except m.GuardError:
        pytest.fail('검토된 10월 8일 엔진 전용 활성화 계약이 필요하다')
    assert profile.allow_predeployed is True
    assert profile.engine_only is True
    assert profile.not_before == datetime(2026, 10, 7, 23, 55, tzinfo=timezone.utc)
    assert profile.latest_start_at == datetime(2026, 10, 7, 23, 57, tzinfo=timezone.utc)
