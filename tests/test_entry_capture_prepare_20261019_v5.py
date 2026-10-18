"""Fifth Monday epoch; every filesystem and service fixture is synthetic."""
import importlib.util
from datetime import datetime, timezone
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_entry_capture_prepare_20261019 as v1
import test_entry_capture_prepare_20261019_v2 as v2
import test_entry_capture_prepare_20261019_v3 as v3
import test_entry_capture_prepare_20261019_v4 as v4
from test_entry_capture_activate import module as activation_module, setup, predeployed_run
from test_entry_capture_activate_20261019 import projection as historical_projection


def projection():
    value = historical_projection()
    value["llm"]["selection_basis_version"] = "selection-basis-v4"
    return value


def payloads(core):
    doc, files = v1.payloads(core)
    study = core.document(files["study"])
    study["capture"]["selection_basis"]["version"] = "selection-basis-v4"
    v1.historical.repin_study(core, doc, files, study)
    return doc, files

SCRIPT = Path(__file__).parents[1] / 'scripts/ops/entry_capture_prepare_20261019_v5.py'


@pytest.fixture
def m():
    assert SCRIPT.exists(), 'separate v5 installer required'
    spec = importlib.util.spec_from_file_location('prepare_oct19_v5', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def installation_clock(m, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 17, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(m.transaction, 'datetime', Clock)
    monkeypatch.setattr(m.core, 'datetime', Clock)


def test_v5_fixed_identities_and_all_four_preserved_inventories(m):
    c = m.core
    prior = v4.m.__wrapped__()
    assert c.PROFILE == '20261019-llm-window-v5'
    assert c.EPOCH == '2026-10-19-engine-llm-window-v5'
    assert c.NOT_BEFORE == prior.core.NOT_BEFORE
    assert len(m.PREDECESSORS) == 4
    assert set(m.OLD_OWNERS) == set(c.OLD)
    assert len(c.OLD) == 332
    assert set(prior.OLD_OWNERS) <= set(c.OLD)
    assert set(prior.core.NEW.values()) <= set(c.OLD)
    assert prior.core.INSTALL/'manifest.json' in c.OLD
    assert all(prior.core.INSTALL/'payload'/key in c.OLD for key in c.NEW)
    assert all(prior.core.BACKUP/f'original-{i}' in c.OLD for i in range(156))
    assert not set(c.NEW.values()) & set(c.OLD)
    assert all(getattr(c, key) != getattr(prior.core, key) for key in
               ('INSTALL', 'BACKUP', 'INPUT', 'ENGINE_STATE', 'STATE', 'ACT_TIMER', 'ACT_SERVICE'))
    doc, files = payloads(c)
    c.validate_manifest(doc)
    c.validate_payload(doc, files)
    assert b'activate-20261019-v5.py --profile 20261019-llm-window-v5' in files['activation_service']
    assert b'OnCalendar=2026-10-19 09:25:00 Asia/Seoul' in files['activation_timer']
    assert {p['manifest_sha256'] for p in m.PREDECESSORS} == {
        'c7aa38ab9ec0d6dcd627551d8baf35c51433a360915bd3d7036ce7601695be96',
        '3b411ac7b75f7f986f5c37a1439c91bd40c83cade1fe43b9480fd24f282375b6',
        '170d956b810862442ef74b60cf000f57a15a3be6bea4f3d663128e2cd5cab915',
        '397450db7d56699b1c13d4122e1eb053ad4f4cbb5a98d1db80a6732d1fdcb63b'}


def test_transaction_source_pin_precedes_execution(m, monkeypatch):
    monkeypatch.setattr(m, 'TRANSACTION_SHA256', '0'*64)
    with pytest.raises(RuntimeError, match='preparation_transaction_pin_mismatch'):
        m._load_transaction()
    assert v2.m.__wrapped__().core.PROFILE == '20261019-llm-window-v2'
    assert v3.m.__wrapped__().core.PROFILE == '20261019-llm-window-v3'
    assert v4.m.__wrapped__().core.PROFILE == '20261019-llm-window-v4'
    assert v1.m.__wrapped__().PROFILE == '20261019-llm-window-v1'


@pytest.mark.parametrize('version', [1, 2, 3, 4])
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


def test_v5_activation_profile_has_strict_readiness():
    a = activation_module()
    selected = a.activation_profile('20261019-llm-window-v5')
    assert selected.engine_only and selected.allow_predeployed
    cfg = SimpleNamespace(engine_only=True, evaluation_epoch=selected.evaluation_epoch)
    assert a.capture_prepared(cfg, projection())
    for key, value in [('selection_max_modifier_stages', 23), ('lease_seconds', 1560),
                       ('market_state_evidence', False), ('selection_basis_version', 'selection-basis-v2'),
                       ('selection_basis_version', 'selection-basis-v3')]:
        invalid = projection(); invalid['llm'][key] = value
        assert not a.capture_prepared(cfg, invalid)
    with pytest.raises(a.GuardError): a.activation_profile('20261019-llm-window-v6')


def test_v5_real_once_consumes_once(m, tmp_path, monkeypatch):
    from src.analytics.entry_observation_startup import claim_once_manifest
    c = m.core
    monkeypatch.setattr(c, 'ENGINE_STATE', tmp_path/'state')
    c.ENGINE_STATE.mkdir(mode=0o700)
    monkeypatch.setattr(c, 'NEW', {**c.NEW, **{key: tmp_path/(key+'.json')
        for key in ('study', 'engine_manifest', 'once')}})
    doc, files = payloads(c)
    c.validate_payload(doc, files)
    for key in ('study', 'engine_manifest', 'once'):
        c.NEW[key].write_bytes(files[key]); c.NEW[key].chmod(0o600)
    now = datetime(2026, 10, 19, 0, 25, tzinfo=timezone.utc)
    assert claim_once_manifest(c.NEW['once'], now=now) == str(c.NEW['engine_manifest'])
    assert claim_once_manifest(c.NEW['once'], now=now) is None


@pytest.mark.parametrize('setup', ['20261019-llm-window-v5'], indirect=True)
@pytest.mark.parametrize('version', [1, 2, 3, 4])
@pytest.mark.parametrize('receipt_index', range(4))
@pytest.mark.parametrize('failure', [None, 'pre_receipt', 'consumed_pre_receipt', 'before_restart', 'consumed_before_restart', 'shared_dropin'])
def test_activation_all_four_predecessor_races(setup, monkeypatch, version, failure, receipt_index):
    s = setup
    attempts = {}
    for v in (1, 2, 3, 4):
        old = s.tmp_path/f'retired-{v}'; old.mkdir(mode=0o700)
        attempts[v] = tuple((old/name, os.getuid()) for name in
                           ('activation.receipt', 'status.json', 'startup.receipt', 'engine.jsonl'))
    monkeypatch.setattr(s.m, 'RETIRED_OCT19_ATTEMPTS', attempts[1])
    monkeypatch.setattr(s.m, 'RETIRED_OCT19_V2_ATTEMPTS', attempts[2], raising=False)
    monkeypatch.setattr(s.m, 'RETIRED_OCT19_V3_ATTEMPTS', attempts[3], raising=False)
    monkeypatch.setattr(s.m, 'RETIRED_OCT19_V4_ATTEMPTS', attempts[4])
    s.source.write_bytes(b'new\n'); s.anchors.update(projection())
    if failure == 'shared_dropin': s.dropin.write_bytes(b'other capture')
    if failure == 'consumed_pre_receipt': attempts[version][receipt_index][0].write_bytes(b'old-consumed')
    checked = []
    state = {'armed': failure == 'pre_receipt'}
    predecessor_units = {'qwq-entry-capture-20261019'+suffix+kind: v
                         for v, suffix in ((1, ''), (2, '-v2'), (3, '-v3'), (4, '-v4')) for kind in ('.timer', '.service')}
    def runner(argv, **kwargs):
        if argv[-1] in predecessor_units:
            v = predecessor_units[argv[-1]]
            checked.append((v, (s.state/'activation.receipt').exists()))
            value = 'disabled' if '--property=UnitFileState' in argv else 'inactive'
            if v == version and state['armed'] and '--property=UnitFileState' in argv: value = 'enabled'
            return SimpleNamespace(returncode=0, stdout=value)
        if 'daemon-reload' in argv and not any('restart' in e for e in s.events):
            if failure == 'before_restart': state['armed'] = True
            if failure == 'consumed_before_restart': attempts[version][receipt_index][0].write_bytes(b'old-consumed')
        return s.runner(argv, **kwargs)
    result = predeployed_run(s, runner=runner)
    assert not any('checkout' in e or 'start' in e for e in s.events)
    if failure in ('pre_receipt', 'consumed_pre_receipt', 'shared_dropin'):
        assert result['status'] == 'rejected' and not (s.state/'activation.receipt').exists()
    elif failure:
        assert result['status'] == 'failed' and (s.state/'activation.receipt').exists()
        assert not s.dropin.exists()
    else:
        assert result['status'] == 'complete'
        assert {v for v, consumed in checked if not consumed} == {1, 2, 3, 4}
        assert {v for v, consumed in checked if consumed} == {1, 2, 3, 4}
    if failure:
        assert not any('restart' in e for e in s.events)
    else:
        assert sum('restart' in e for e in s.events) == 1
    if failure in ('consumed_pre_receipt', 'consumed_before_restart'): assert attempts[version][receipt_index][0].read_bytes() == b'old-consumed'
    if failure == 'shared_dropin': assert s.dropin.read_bytes() == b'other capture'


@pytest.mark.parametrize('defect', [None, 'deploy', 'old_paths', 'old_epoch'])
def test_v5_activation_config_fixed_contract(m, monkeypatch, defect):
    a = activation_module(); doc, files = payloads(m.core); raw = m.core.document(files['activation'])
    if defect == 'deploy': raw['new_head'] = 'b'*40
    if defect == 'old_paths': raw['once_path'] = raw['once_path'].replace('-v5/', '-v4/')
    if defect == 'old_epoch': raw['evaluation_epoch'] = '2026-10-19-engine-llm-window-v2'
    monkeypatch.setattr(a.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(a, 'trusted_dir', lambda *args: None)
    monkeypatch.setattr(a, 'read_file', lambda *args, **kwargs: m.core.raw(raw))
    if defect:
        with pytest.raises(a.GuardError): a.load_config(m.core.PROFILE)
    else:
        cfg = a.load_config(m.core.PROFILE)
        assert cfg.old_head == cfg.new_head == doc['engine_head']


@pytest.mark.parametrize('argv,accepted', [(['--profile', '20261019-llm-window-v5'], True),
    (['--profile', '20261019-llm-window-v5', '--date', '2026-10-20'], False),
    (['--profile', '20261019-llm-window-v6'], False)])
def test_v5_cli_is_fixed(monkeypatch, capsys, argv, accepted):
    a = activation_module(); selected = []
    monkeypatch.setattr(a.sys, 'argv', [a.__file__, *argv])
    monkeypatch.setattr(a, 'load_config', lambda profile: selected.append(profile) or profile)
    monkeypatch.setattr(a, 'activate', lambda cfg: {'status': 'complete'})
    assert a.main() == (0 if accepted else 1)
    assert selected == (['20261019-llm-window-v5'] if accepted else [])


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


@pytest.mark.parametrize('version', [1, 2, 3, 4])
@pytest.mark.parametrize('defect', [None, 'pin', 'known_manifest', 'installed', 'staged', 'backup'])
def test_all_four_packages_are_immutable(m, installed_predecessors, version, defect):
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
                    (f'20261019-v{version}/' in str(p) if version > 1 else '20261019/' in str(p)))
        path.write_bytes(b'changed')
    if defect:
        with pytest.raises(m.core.GuardError): m.old_inputs(s.doc)
    else: assert m.old_inputs(s.doc) == s.originals


@pytest.mark.parametrize('failure', [None, 'check', 'static', 'enable', 'backup_write',
    'disk_before', 'disk_locked', 'disk_arm', 'disarm', 'old_consumed', 'old_rearmed', 'old_changed'])
@pytest.mark.parametrize('version', [1, 2, 3, 4])
def test_v5_transaction_preserves_all_four_and_disarms_on_failure(m, installed_predecessors, tmp_path, monkeypatch, failure, version, failure_at=6):
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
                if prop == 'UnitFileState': return 'enabled' if self.old_enabled and unit == m.PREDECESSORS[version-1]['timer'] else 'disabled'
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
                if failure == 'old_changed': m.PREDECESSORS[version-1]['files']['once'].write_bytes(b'changed')
            if 'disable' in args:
                if failure == 'disarm': raise OSError('disable failed')
                self.enabled = False
    host = Host()
    check_count = 0
    original_retirement = m.retirement_guards
    def retirement_boundary(current_host):
        nonlocal check_count
        check_count += 1
        if check_count == failure_at:
            if failure == 'old_rearmed': current_host.old_enabled = True
            if failure == 'old_consumed':
                index = (version-1)*4 if version == 1 else (version-1)*4+1
                list(absent)[index].write_bytes(b'consumed')
        original_retirement(current_host)
    monkeypatch.setattr(m.transaction, 'retirement_guards', retirement_boundary)
    if failure in ('old_consumed', 'old_rearmed') and failure_at <= 2:
        reason = 'predecessor_attempt_or_dropin_exists' if failure == 'old_consumed' else 'predecessor_not_quiescent'
        with pytest.raises(c.GuardError, match=reason): m.prepare('a'*64, apply=True, host=host)
        assert not c.BACKUP.exists() and not host.commands
    elif failure in ('disk_before', 'disk_locked'):
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
            assert len(index) == 332
            for p, data in s.originals.items(): assert (c.BACKUP/index[str(p)]['file']).read_bytes() == data
            with pytest.raises(c.GuardError, match='preparation_already_attempted'): m.prepare('a'*64, apply=True, host=host)
    for p, data in s.originals.items():
        if failure != 'old_changed' or p != m.PREDECESSORS[version-1]['files']['once']: assert p.read_bytes() == data
    assert not c.DROPIN.exists()
    assert not any(word in args for args in host.commands for word in ('restart', 'start', c.TOSS, *predecessors))


def test_retirement_checks_all_four_attempt_sets_and_shared_dropin(m, monkeypatch):
    seen = []
    monkeypatch.setattr(m, 'require_absent', lambda p, owner: seen.append((p, owner)))
    host = SimpleNamespace(show=lambda unit, prop: 'disabled' if prop == 'UnitFileState' else 'inactive')
    m.retirement_guards(host)
    assert dict(seen) == m.OLD_ABSENT
    assert len(seen) == 17
    for version in (1, 2, 3, 4):
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


def test_v5_cli_help_identifies_correct_installation(m, capsys):
    with pytest.raises(SystemExit) as exit_info:
        m.main(['--help'])
    assert exit_info.value.code == 0
    assert 'Fixed October 19 v5 capture' in capsys.readouterr().out


@pytest.mark.parametrize('instant', ['2026-10-19T00:20:00+00:00',
                                    '2026-10-19T00:25:00+00:00',
                                    '2027-01-01T00:00:00+00:00'])
@pytest.mark.parametrize('apply', [False, True])
def test_v5_deadline_rejects_before_host_or_file_reads(m, monkeypatch, instant, apply):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(instant).astimezone(tz)
    monkeypatch.setattr(m.transaction, 'datetime', Clock)
    monkeypatch.setattr(m.core, 'load_manifest', lambda _: pytest.fail('manifest read after deadline'))
    monkeypatch.setattr(m.core, 'Host', lambda: pytest.fail('host created after deadline'))
    with pytest.raises(m.core.GuardError, match='installation_deadline_passed'):
        m.prepare('a'*64, apply=apply)


@pytest.mark.parametrize('version', ['selection-basis-v2', 'selection-basis-v3', 'selection-basis-v5', None])
def test_v5_rejects_non_v4_study_even_when_rehashed(m, version):
    doc, files = payloads(m.core)
    study = m.core.document(files['study'])
    study['capture']['selection_basis']['version'] = version
    v1.historical.repin_study(m.core, doc, files, study)
    with pytest.raises(m.core.GuardError, match='selection_contract_invalid'):
        m.core.validate_payload(doc, files)


def test_v5_study_adapter_preserves_actual_bytes_and_all_other_guards(m):
    import copy
    doc, files = payloads(m.core)
    study = m.core.document(files['study']); original = copy.deepcopy(study)
    m.core.validate_study(study, doc['engine_head'])
    assert study == original
    assert files['study'] == m.core.raw(original)
    for scope, key, invalid in [('selection_basis', 'max_modifier_stages', 23),
                                ('selection_basis', 'max_candidates', 99),
                                ('channels', 'market_state_evidence', False)]:
        bad = copy.deepcopy(study); bad['capture'][scope][key] = invalid
        with pytest.raises(m.core.GuardError): m.core.validate_study(bad, doc['engine_head'])
    previous = v4.m.__wrapped__().core
    old_doc, old_files = v1.payloads(previous)
    previous.validate_payload(old_doc, old_files)
    with pytest.raises(previous.GuardError):
        previous.validate_study(study, doc['engine_head'])


@pytest.mark.parametrize('version', [1, 2, 3, 4])
@pytest.mark.parametrize('failure', ['old_rearmed', 'old_consumed'])
@pytest.mark.parametrize('failure_at', [1, 2, 3, 4, 5])
def test_v5_rechecks_predecessors_at_each_install_boundary(
        m, installed_predecessors, tmp_path, monkeypatch, version, failure, failure_at):
    test_v5_transaction_preserves_all_four_and_disarms_on_failure(
        m, installed_predecessors, tmp_path, monkeypatch, failure, version, failure_at)
