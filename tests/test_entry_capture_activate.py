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


def module():
    assert SCRIPT.is_file(), 'bounded activation driver is missing'
    spec = importlib.util.spec_from_file_location('entry_capture_activate', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def setup(tmp_path):
    m = module()
    now = datetime(2026, 10, 1, 23, 55, tzinfo=timezone.utc)
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
    once = tmp_path / 'once.json'
    once.write_bytes(b'{}')
    launcher, deployment = tmp_path / 'launcher.py', tmp_path / 'deployment.json'
    launcher.write_bytes(b'launcher')
    deployment.write_bytes(b'{}')
    kill = tmp_path / 'KILL_SWITCH_KR'
    kill.touch()
    state = tmp_path / 'state'
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
        evaluation_epoch='kr-entry-20261002-firstscan-v1')
    events = []
    current = [now]
    health = {'broker': {'connected': True, 'pending_orders': 0},
              'risk_manager': {'pending_orders': 0, 'pending_quantities': 0, 'pending_sells': 0}}
    anchors = {'schema_version':'entry-anchor-projection-v2', 'study_sha256':'c'*64,
               'evaluation_epoch':cfg.evaluation_epoch, 'capture_id':'capture-1',
               'capture_closed':False, 'source_record_count':0, 'records':[]}
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
        if path.endswith('entry-anchors'):
            behavior['anchor_attempts'] = behavior.get('anchor_attempts',0)+1
            if behavior['anchor_attempts'] <= behavior.get('ready_after',0):
                raise ValueError('still starting')
        if behavior.get('fetch_fail') and path.endswith('entry-anchors'):
            raise ValueError('secret account output')
        return health if path == '/api/health' else anchors

    def run():
        return m.activate(cfg, runner=runner, clock=lambda:current[0], fetch=fetch,
                          sleep=lambda seconds:current.__setitem__(0,current[0]+timedelta(seconds=seconds)), trusted_uid=os.getuid(), lock_uid=os.getuid())
    return SimpleNamespace(**locals())


@pytest.mark.parametrize('offset', [-1, 120, 121])
def test_outside_window_has_no_receipt_or_commands(setup, offset):
    s = setup
    s.current[0] = s.now + timedelta(seconds=offset)
    assert s.run()['status'] == 'rejected'
    assert not s.events
    assert not (s.state / 'activation.receipt').exists()


def test_success_orders_check_checkout_reload_restart_health_then_toss(setup):
    s = setup
    result = s.run()
    assert result['status'] == 'complete', (result, s.events)
    assert s.source.read_bytes() == b'new\n'
    assert s.dropin.read_bytes() == s.staged.read_bytes()
    commands = s.events
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
    def hook(argv):
        if 'start' in argv:
            s.current[0] = s.now+timedelta(seconds=120)
    s.behavior['hook'] = hook
    assert s.run()['status'] == 'failed'
    assert sum('start' in c for c in s.events) == 1
    assert sum('restart' in c for c in s.events) == 1


@pytest.mark.parametrize('problem', [None, 'credentials', 'repo', 'time', 'group', 'source'])
def test_fixed_configuration_contract(setup, monkeypatch, problem):
    from dataclasses import asdict
    s = setup
    m = s.m
    raw = asdict(s.cfg)
    fixed = {'repo':m.REPO, 'once_path':m.ONCE_PATH, 'launcher_path':m.LAUNCHER_PATH,
             'deployment_path':m.DEPLOYMENT_PATH,'staged_dropin':m.STAGED_DROPIN,
             'dropin_path':m.DROPIN_PATH,'kill_path':m.KILL_PATH,'state_dir':m.STATE_DIR,
             'lock_path':m.LOCK_PATH}
    raw.update({k:str(v) for k,v in fixed.items()})
    raw['not_before'] = '2026-10-01T23:55:00+00:00'
    raw['latest_start_at'] = '2026-10-01T23:57:00+00:00'
    paths = [m.ONCE_PATH, m.ONCE_PATH.with_name('study.json'),m.ONCE_PATH.with_name('manifest.json'),
             m.LAUNCHER_PATH,m.DEPLOYMENT_PATH,Path('/etc/qwq-toss-observer/plan.json'),
             Path('/etc/qwq-toss-observer/registry.json')]
    raw['input_hashes'] = {str(p):'e'*64 for p in paths}
    if problem == 'credentials': raw['input_hashes']['/etc/qwq-toss-observer/credentials.env'] = 'f'*64
    if problem == 'repo': raw['repo'] = '/tmp/other'
    if problem == 'time': raw['latest_start_at'] = '2026-10-01T23:58:00+00:00'
    if problem == 'group': raw['toss_gid'] = 0
    if problem == 'source': raw['source_hashes']['.env'] = 'f'*64
    config = s.tmp_path/'config.json'
    config.write_text(json.dumps(raw))
    config.chmod(0o600)
    monkeypatch.setattr(m,'CONFIG_PATH',config)
    monkeypatch.setattr(m.os,'geteuid',lambda:0)
    monkeypatch.setattr(m,'trusted_dir',lambda *args:None)
    original = m.read_file
    monkeypatch.setattr(m,'read_file',lambda p,**kw:original(p,owner=os.getuid()))
    if problem:
        with pytest.raises(m.GuardError): m.load_config()
    else:
        assert m.load_config().toss_gid == 987


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
