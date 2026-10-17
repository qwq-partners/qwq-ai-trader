"""Third Monday epoch; every filesystem and service fixture is synthetic."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_entry_capture_prepare_20261019 as v1
import test_entry_capture_prepare_20261019_v2 as v2
from test_entry_capture_activate import module as activation_module, setup, predeployed_run
from test_entry_capture_activate_20261019 import projection

SCRIPT = Path(__file__).parents[1] / 'scripts/ops/entry_capture_prepare_20261019_v3.py'


@pytest.fixture
def m():
    assert SCRIPT.exists(), 'separate v3 installer required'
    spec = importlib.util.spec_from_file_location('prepare_oct19_v3', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_v3_fixed_identities_and_both_preserved_inventories(m):
    c = m.core
    prior = v2.m.__wrapped__()
    assert c.PROFILE == '20261019-llm-window-v3'
    assert c.EPOCH == '2026-10-19-engine-llm-window-v3'
    assert c.NOT_BEFORE == prior.core.NOT_BEFORE
    assert len(m.PREDECESSORS) == 2
    assert set(m.OLD_OWNERS) == set(c.OLD)
    assert len(c.OLD) == 68
    assert set(prior.OLD_OWNERS) <= set(c.OLD)
    assert set(prior.core.NEW.values()) <= set(c.OLD)
    assert prior.core.INSTALL/'manifest.json' in c.OLD
    assert all(prior.core.INSTALL/'payload'/key in c.OLD for key in c.NEW)
    assert all(prior.core.BACKUP/f'original-{i}' in c.OLD for i in range(24))
    assert not set(c.NEW.values()) & set(c.OLD)
    assert all(getattr(c, key) != getattr(prior.core, key) for key in
               ('INSTALL', 'BACKUP', 'INPUT', 'ENGINE_STATE', 'STATE', 'ACT_TIMER', 'ACT_SERVICE'))
    doc, files = v1.payloads(c)
    c.validate_manifest(doc)
    c.validate_payload(doc, files)
    assert b'activate-20261019-v3.py --profile 20261019-llm-window-v3' in files['activation_service']
    assert b'OnCalendar=2026-10-19 09:25:00 Asia/Seoul' in files['activation_timer']
    assert {p['manifest_sha256'] for p in m.PREDECESSORS} == {
        'c7aa38ab9ec0d6dcd627551d8baf35c51433a360915bd3d7036ce7601695be96',
        '3b411ac7b75f7f986f5c37a1439c91bd40c83cade1fe43b9480fd24f282375b6'}


def test_transaction_source_pin_precedes_execution(m, monkeypatch):
    monkeypatch.setattr(m, 'TRANSACTION_SHA256', '0'*64)
    with pytest.raises(RuntimeError, match='preparation_transaction_pin_mismatch'):
        m._load_transaction()
    assert v2.m.__wrapped__().core.PROFILE == '20261019-llm-window-v2'
    assert v1.m.__wrapped__().PROFILE == '20261019-llm-window-v1'


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('failure', ['active', 'enabled', 'service'])
def test_each_predecessor_must_be_quiescent(m, monkeypatch, version, failure):
    monkeypatch.setattr(m, 'require_absent', lambda *args: None)
    target = m.PREDECESSORS[version-1]
    class Host:
        def show(self, unit, prop):
            if unit == target['timer'] and prop == 'ActiveState' and failure == 'active': return 'active'
            if unit == target['timer'] and prop == 'UnitFileState' and failure == 'enabled': return 'enabled'
            if unit == target['service'] and failure == 'service': return 'activating'
            return 'disabled' if prop == 'UnitFileState' else 'inactive'
    with pytest.raises(m.core.GuardError, match='predecessor_not_quiescent'):
        m.retirement_guards(Host())


def test_v3_activation_profile_has_strict_readiness():
    a = activation_module()
    selected = a.activation_profile('20261019-llm-window-v3')
    assert selected.engine_only and selected.allow_predeployed
    cfg = SimpleNamespace(engine_only=True, evaluation_epoch=selected.evaluation_epoch)
    assert a.capture_prepared(cfg, projection())
    for key, value in [('selection_max_modifier_stages', 23), ('lease_seconds', 1560),
                       ('market_state_evidence', False), ('selection_basis_version', 'selection-basis-v2')]:
        invalid = projection(); invalid['llm'][key] = value
        assert not a.capture_prepared(cfg, invalid)
    with pytest.raises(a.GuardError): a.activation_profile('20261019-llm-window-v4')


@pytest.mark.asyncio
async def test_v3_real_once_and_runtime_loader(m, tmp_path, monkeypatch):
    await v2.test_v2_real_once_and_runtime_readiness(m, tmp_path, monkeypatch)


@pytest.mark.parametrize('setup', ['20261019-llm-window-v3'], indirect=True)
@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('failure', [None, 'pre_receipt', 'before_restart', 'consumed_before_restart', 'shared_dropin'])
def test_activation_both_predecessor_races(setup, monkeypatch, version, failure):
    s = setup
    attempts = {}
    for v in (1, 2):
        old = s.tmp_path/f'retired-{v}'; old.mkdir(mode=0o700)
        attempts[v] = tuple((old/name, os.getuid()) for name in
                           ('activation.receipt', 'status.json', 'startup.receipt', 'engine.jsonl'))
    monkeypatch.setattr(s.m, 'RETIRED_OCT19_ATTEMPTS', attempts[1])
    monkeypatch.setattr(s.m, 'RETIRED_OCT19_V2_ATTEMPTS', attempts[2], raising=False)
    s.source.write_bytes(b'new\n'); s.anchors.update(projection())
    if failure == 'shared_dropin': s.dropin.write_bytes(b'other capture')
    checked = []
    state = {'armed': failure == 'pre_receipt'}
    predecessor_units = {'qwq-entry-capture-20261019'+suffix+kind: v
                         for v, suffix in ((1, ''), (2, '-v2')) for kind in ('.timer', '.service')}
    def runner(argv, **kwargs):
        if argv[-1] in predecessor_units:
            v = predecessor_units[argv[-1]]
            checked.append((v, (s.state/'activation.receipt').exists()))
            value = 'disabled' if '--property=UnitFileState' in argv else 'inactive'
            if v == version and state['armed'] and '--property=UnitFileState' in argv: value = 'enabled'
            return SimpleNamespace(returncode=0, stdout=value)
        if 'daemon-reload' in argv and not any('restart' in e for e in s.events):
            if failure == 'before_restart': state['armed'] = True
            if failure == 'consumed_before_restart': attempts[version][0][0].write_bytes(b'old-consumed')
        return s.runner(argv, **kwargs)
    result = predeployed_run(s, runner=runner)
    assert not any('checkout' in e or 'start' in e for e in s.events)
    if failure in ('pre_receipt', 'shared_dropin'):
        assert result['status'] == 'rejected' and not (s.state/'activation.receipt').exists()
    elif failure:
        assert result['status'] == 'failed' and (s.state/'activation.receipt').exists()
        assert not s.dropin.exists()
    else:
        assert result['status'] == 'complete'
        assert {v for v, consumed in checked if not consumed} == {1, 2}
        assert {v for v, consumed in checked if consumed} == {1, 2}
    if failure:
        assert not any('restart' in e for e in s.events)
    else:
        assert sum('restart' in e for e in s.events) == 1
    if failure == 'consumed_before_restart': assert attempts[version][0][0].read_bytes() == b'old-consumed'
    if failure == 'shared_dropin': assert s.dropin.read_bytes() == b'other capture'


@pytest.mark.parametrize('defect', [None, 'deploy', 'old_paths', 'old_epoch'])
def test_v3_activation_config_fixed_contract(m, monkeypatch, defect):
    a = activation_module(); doc, files = v1.payloads(m.core); raw = m.core.document(files['activation'])
    if defect == 'deploy': raw['new_head'] = 'b'*40
    if defect == 'old_paths': raw['once_path'] = raw['once_path'].replace('-v3/', '-v2/')
    if defect == 'old_epoch': raw['evaluation_epoch'] = '2026-10-19-engine-llm-window-v2'
    monkeypatch.setattr(a.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(a, 'trusted_dir', lambda *args: None)
    monkeypatch.setattr(a, 'read_file', lambda *args, **kwargs: m.core.raw(raw))
    if defect:
        with pytest.raises(a.GuardError): a.load_config(m.core.PROFILE)
    else:
        cfg = a.load_config(m.core.PROFILE)
        assert cfg.old_head == cfg.new_head == doc['engine_head']


@pytest.mark.parametrize('argv,accepted', [(['--profile', '20261019-llm-window-v3'], True),
    (['--profile', '20261019-llm-window-v3', '--date', '2026-10-20'], False),
    (['--profile', '20261019-llm-window-v4'], False)])
def test_v3_cli_is_fixed(monkeypatch, capsys, argv, accepted):
    a = activation_module(); selected = []
    monkeypatch.setattr(a.sys, 'argv', [a.__file__, *argv])
    monkeypatch.setattr(a, 'load_config', lambda profile: selected.append(profile) or profile)
    monkeypatch.setattr(a, 'activate', lambda cfg: {'status': 'complete'})
    assert a.main() == (0 if accepted else 1)
    assert selected == (['20261019-llm-window-v3'] if accepted else [])


@pytest.fixture
def installed_predecessors(m, tmp_path, monkeypatch):
    c = m.core
    def relocated(p): return tmp_path/'old'/str(p).lstrip('/')
    owners = {relocated(p): owner for p, owner in m.OLD_OWNERS.items()}
    predecessors = [{**p, 'install': relocated(p['install']),
                     'files': {k: relocated(v) for k, v in p['files'].items()}}
                    for p in m.PREDECESSORS]
    originals = {p: ('original:'+str(p)).encode() for p in owners}
    for prior in predecessors:
        for key, path in prior['files'].items(): originals[prior['install']/'payload'/key] = originals[path]
        manifest = c.raw({'new_files': {k: c.sha(originals[p]) for k, p in prior['files'].items()}})
        originals[prior['install']/'manifest.json'] = manifest
        prior['manifest_sha256'] = c.sha(manifest)
    for path, data in originals.items():
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    monkeypatch.setattr(m, 'PREDECESSORS', tuple(predecessors))
    monkeypatch.setattr(m, 'OLD_OWNERS', owners)
    monkeypatch.setattr(c, 'trusted_read', lambda p, **kw: Path(p).read_bytes())
    doc = {'old_files': {str(p): c.sha(data) for p, data in originals.items()}}
    return SimpleNamespace(originals=originals, doc=doc, predecessors=predecessors)


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('defect', [None, 'pin', 'known_manifest', 'installed', 'staged', 'backup'])
def test_both_packages_are_immutable(m, installed_predecessors, version, defect):
    s = installed_predecessors; prior = s.predecessors[version-1]
    if defect == 'pin': s.doc['old_files'][str(next(iter(prior['files'].values())))] = '0'*64
    if defect == 'known_manifest': prior['manifest_sha256'] = '0'*64
    if defect in ('installed', 'staged'):
        path = prior['files']['once'] if defect == 'installed' else prior['install']/'payload'/'once'
        path.write_bytes(b'changed')
        # Even a newly rehashed installation manifest cannot disguise a changed old package.
        s.doc['old_files'][str(path)] = m.core.sha(path.read_bytes())
    if defect == 'backup':
        path = next(p for p in s.originals if p.name == 'original-0' and
                    ('20261019-v2' in str(p) if version == 2 else '20261019/' in str(p)))
        path.write_bytes(b'changed')
    if defect:
        with pytest.raises(m.core.GuardError): m.old_inputs(s.doc)
    else: assert m.old_inputs(s.doc) == s.originals


@pytest.mark.parametrize('failure', [None, 'check', 'static', 'enable', 'backup_write',
    'disk_before', 'disk_locked', 'disk_arm', 'disarm', 'old_consumed', 'old_rearmed', 'old_changed'])
def test_v3_transaction_preserves_both_and_disarms_on_failure(m, installed_predecessors, tmp_path, monkeypatch, failure):
    c = m.core; s = installed_predecessors
    for name in ('INSTALL', 'BACKUP', 'INPUT', 'ENGINE_STATE', 'STATE', 'DROPIN', 'LOCK'):
        monkeypatch.setattr(c, name, tmp_path/name)
    monkeypatch.setattr(c, 'NEW', {key: tmp_path/('new-'+key) for key in c.NEW})
    absent = {tmp_path/f'old-absent-{i}': owner for i, owner in enumerate(m.OLD_ABSENT.values())}
    absent[c.DROPIN] = 0
    monkeypatch.setattr(m, 'OLD_ABSENT', absent)
    c.INSTALL.mkdir(); (c.INSTALL/'payload').mkdir()
    files = {key: ('new-'+key).encode() for key in c.NEW}
    for key, data in files.items(): (c.INSTALL/'payload'/key).write_bytes(data)
    c.LOCK.write_bytes(b''); c.LOCK.chmod(0o644)
    monkeypatch.setattr(c, 'load_manifest', lambda _: s.doc)
    monkeypatch.setattr(c, 'validate_payload', lambda *args: None)
    monkeypatch.setattr(c, 'runtime_guards', lambda *args, **kwargs: None)
    monkeypatch.setattr(c, 'mkdir_new', lambda p, owner=0: p.mkdir(mode=0o700))
    monkeypatch.setattr(c, 'open_dir', lambda p, owner=0: os.open(p, os.O_RDONLY))
    original_write = c.write_new
    def write(p, data, mode=0o600, owner=0):
        if failure == 'backup_write' and p.name == 'attempt.json': raise OSError('injected')
        original_write(p, data, mode, os.geteuid())
    monkeypatch.setattr(c, 'write_new', write)
    real_fstat = os.fstat
    lock_stat = SimpleNamespace(st_mode=0o100644, st_uid=1000, st_nlink=1)
    monkeypatch.setattr(os, 'fstat', lambda fd: lock_stat if os.readlink('/proc/self/fd/'+str(fd)) == str(c.LOCK) else real_fstat(fd))
    predecessors = {p[k] for p in m.PREDECESSORS for k in ('timer', 'service')}
    class Host:
        enabled = False; old_enabled = False; disk_checks = 0
        def __init__(self): self.commands = []
        def show(self, unit, prop):
            if unit in predecessors:
                if prop == 'UnitFileState': return 'enabled' if self.old_enabled and unit == m.PREDECESSORS[1]['timer'] else 'disabled'
                return 'inactive'
            if prop == 'ActiveState': return 'active' if self.enabled and unit == c.ACT_TIMER else 'inactive'
            if prop == 'UnitFileState': return 'enabled' if self.enabled else 'disabled'
            if prop == 'Unit': return c.ACT_SERVICE
            if prop == 'FragmentPath': return str(c.NEW['activation_timer' if unit == c.ACT_TIMER else 'activation_service'])
            if prop == 'DropInPaths': return ''
            if prop == 'NextElapseUSecRealtime': return 'Mon 2026-10-19 09:25:00 KST'
            raise AssertionError((unit, prop))
        def disk_available(self, path):
            self.disk_checks += 1
            return 0 if self.disk_checks == {'disk_before': 1, 'disk_locked': 2, 'disk_arm': 3}.get(failure) else c.MIN_DISK_AVAILABLE_BYTES
        def static(self):
            if failure == 'static': raise c.GuardError('injected')
        def run(self, args):
            self.commands.append(args)
            if 'enable' in args:
                self.enabled = True
                if failure in ('enable', 'disarm'): raise OSError('enable failed')
                if failure == 'old_consumed': next(iter(absent)).write_bytes(b'consumed')
                if failure == 'old_rearmed': self.old_enabled = True
                if failure == 'old_changed': m.PREDECESSORS[1]['files']['once'].write_bytes(b'changed')
            if 'disable' in args:
                if failure == 'disarm': raise OSError('disable failed')
                self.enabled = False
    host = Host()
    if failure in ('disk_before', 'disk_locked'):
        with pytest.raises(c.GuardError, match='journal_disk_capacity_insufficient'): m.prepare('a'*64, apply=True, host=host)
        assert not c.BACKUP.exists() and not host.commands
    elif failure not in (None, 'check'):
        expected = 'preparation_disarm_failed' if failure == 'disarm' else 'preparation_partial_disarmed'
        with pytest.raises(c.GuardError, match=expected): m.prepare('a'*64, apply=True, host=host)
        assert host.enabled is (failure == 'disarm')
        assert c.document((c.BACKUP/'failed.json').read_bytes())['new_timer_disarmed'] is (failure != 'disarm')
        with pytest.raises(c.GuardError, match='preparation_already_attempted'): m.prepare('a'*64, apply=True, host=host)
    else:
        result = m.prepare('a'*64, apply=failure != 'check', host=host)
        assert result['applied'] is (failure != 'check')
        if failure == 'check': assert not c.BACKUP.exists() and not host.commands
        else:
            assert host.enabled and (c.BACKUP/'complete.json').exists()
            index = c.document((c.BACKUP/'index.json').read_bytes())
            assert len(index) == 68
            for p, data in s.originals.items(): assert (c.BACKUP/index[str(p)]['file']).read_bytes() == data
            with pytest.raises(c.GuardError, match='preparation_already_attempted'): m.prepare('a'*64, apply=True, host=host)
    for p, data in s.originals.items():
        if failure != 'old_changed' or p != m.PREDECESSORS[1]['files']['once']: assert p.read_bytes() == data
    assert not c.DROPIN.exists()
    assert not any(word in args for args in host.commands for word in ('restart', 'start', c.TOSS, *predecessors))


def test_retirement_checks_both_attempt_sets_and_shared_dropin(m, monkeypatch):
    seen = []
    monkeypatch.setattr(m, 'require_absent', lambda p, owner: seen.append((p, owner)))
    host = SimpleNamespace(show=lambda unit, prop: 'disabled' if prop == 'UnitFileState' else 'inactive')
    m.retirement_guards(host)
    assert dict(seen) == m.OLD_ABSENT
    assert len(seen) == 9
    for version in (1, 2):
        paths = {p.name for p, _ in seen if f'20261019-llm-window-v{version}/' in str(p)}
        assert paths == {'activation.receipt', 'status.json', 'startup.receipt', 'engine.jsonl'}
    assert (m.core.DROPIN, 0) in seen


@pytest.mark.parametrize('kind', ['file', 'symlink', 'dangling'])
def test_absence_never_follows_symlink(m, tmp_path, monkeypatch, kind):
    p = tmp_path/'receipt'
    if kind == 'file': p.write_bytes(b'consumed')
    else: p.symlink_to(tmp_path/('receipt' if kind == 'symlink' else 'missing'))
    monkeypatch.setattr(m.core, 'open_dir', lambda path, owner: os.open(path, os.O_RDONLY))
    with pytest.raises(m.core.GuardError, match='predecessor_attempt_or_dropin_exists'):
        m.require_absent(p, 0)


def test_v3_cli_help_identifies_correct_installation(m, capsys):
    with pytest.raises(SystemExit) as exit_info:
        m.main(['--help'])
    assert exit_info.value.code == 0
    assert 'Fixed October 19 v3 capture' in capsys.readouterr().out
