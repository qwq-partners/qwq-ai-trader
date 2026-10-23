"""Offline report packaging and install preparation never own an observer service."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

def api(name):
    path = ROOT / 'scripts/ops/toss_report' / (name + '.py')
    assert path.exists(), f'report {name} missing'
    spec = importlib.util.spec_from_file_location('report_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_package_closure_includes_protocol_and_helper_excludes_trading():
    module = api('package_release')
    names = {str(p.relative_to(ROOT)) for p in module.source_files(ROOT)}
    assert 'src/observation/toss_report_helper.py' in names
    assert 'src/data/providers/toss/report_protocol.py' in names
    assert 'scripts/run_trader.py' not in names
    assert not any('/broker' in p or '/engine' in p or '.env' in p for p in names)


def test_report_installer_dry_run_has_no_target_mutations(tmp_path, monkeypatch):
    module = api('install')
    def fail(*a, **kw): pytest.fail('dry-run attempted provisioning')
    monkeypatch.setattr(module, 'provision', fail)
    monkeypatch.setattr(module, 'inspect_inputs', lambda **kw: {'artifact_sha256': 'a' * 64})
    monkeypatch.setattr(module, 'check_targets', lambda digest: None)
    result = module.install(artifact=tmp_path, deployment_source=tmp_path/'deployment.json',
        registry_source=tmp_path/'registry.json', plan_source=tmp_path/'plan.json', expected_digest='a'*64)
    assert result == {'ready': True, 'applied': False, 'artifact_sha256': 'a'*64}
    assert list(tmp_path.iterdir()) == []


def test_report_installer_never_overwrites_existing_target(tmp_path, monkeypatch):
    module = api('install')
    monkeypatch.setattr(module, 'PREFIX', tmp_path)
    (tmp_path / 'etc/qwq-toss-report').mkdir(parents=True)
    with pytest.raises(module.InstallError): module.check_targets('a'*64)


def prepared_inputs(approved_release, tmp_path):
    from datetime import datetime, timedelta, timezone
    import hashlib
    launcher, document, plan, grant = approved_release
    installer = api('install')
    root = Path(document['release_root'])
    info = installer.inspect_artifact(root)
    config, state = Path('/etc/qwq-toss-report'), Path('/var/lib/qwq-toss-report')
    installed = Path('/opt/qwq-toss-report/releases') / info['artifact_sha256']
    cohort = state/'cohorts'/document['plan_canonical_hash']
    document.update(release_root=str(installed), manifest_path=str(installed/'manifest.json'),
        registry_path=str(config/'registry.json'), plan_path=str(config/'plan.json'),
        state_directory=str(state), token_directory=str(state/'tokens'), sender_lock_path=str(state/'sender.lock'),
        ledger_path=str(cohort/'observations.jsonl'), status_path=str(cohort/'status.json'), receipt_directory=str(state/'starts'))
    document['config_hash'] = launcher.configuration_hash(document)
    for key in ('config_hash','token_directory','sender_lock_path','ledger_path'): grant[key] = document[key]
    files = {key:tmp_path/(key+'.json') for key in ('deployment','registry','plan')}
    files['deployment'].write_bytes(launcher.canonical_bytes(document))
    files['registry'].write_bytes(launcher.canonical_bytes(dict(schema_version=1,grants=[grant])))
    files['plan'].write_bytes(Path(approved_release[0].CONFIG_DIRECTORY/'plan.json').read_bytes())
    args = dict(artifact=root, deployment_source=files['deployment'], registry_source=files['registry'],
                plan_source=files['plan'], expected_digest=info['artifact_sha256'])
    return installer, args, document, grant, files


from test_toss_report_launcher import approved_release, sealed_release


def test_real_install_inputs_preserve_separate_namespace_without_writes(approved_release, tmp_path):
    installer, args, document, _, _ = prepared_inputs(approved_release, tmp_path)
    result = installer.inspect_inputs(**args)
    assert result['deployment']['token_directory'] == '/var/lib/qwq-toss-report/tokens'
    assert result['deployment']['service_uid'] == document['service_uid']


@pytest.mark.parametrize('change', ['observer_state','wrong_grant','old_grant','plan_bytes','digest'])
def test_real_install_inputs_reject_rebinding(approved_release, tmp_path, change):
    installer, args, document, grant, files = prepared_inputs(approved_release, tmp_path)
    import json
    if change == 'observer_state':
        document['state_directory'] = '/var/lib/qwq-toss-observer'
        document['config_hash'] = installer.helper().configuration_hash(document)
        files['deployment'].write_bytes(installer.helper().canonical_bytes(document))
    elif change in ('wrong_grant','old_grant'):
        grant['grant_id' if change == 'wrong_grant' else 'schema_version'] = 'wrong' if change == 'wrong_grant' else 1
        files['registry'].write_text(json.dumps(dict(schema_version=1,grants=[grant])))
    elif change == 'plan_bytes': files['plan'].write_bytes(files['plan'].read_bytes()+b'\n')
    else: args['expected_digest'] = '0'*64
    with pytest.raises(installer.InstallError): installer.inspect_inputs(**args)


def test_provision_uses_only_new_report_tree_and_preserves_existing_namespace(approved_release, tmp_path, monkeypatch):
    installer, args, document, _, _ = prepared_inputs(approved_release, tmp_path)
    info = installer.inspect_inputs(**args)
    destination = tmp_path/'install'
    destination.mkdir()
    old = destination/'var/lib/qwq-toss-observer'
    old.mkdir(parents=True)
    (old/'preserved').write_text('old observation')
    monkeypatch.setattr(installer, 'PREFIX', destination)
    owned = []
    monkeypatch.setattr(installer.os, 'chown', lambda path,uid,gid: owned.append((Path(path),uid,gid)))
    installer.provision(args['artifact'], info)
    assert (old/'preserved').read_text() == 'old observation'
    assert len(list(old.iterdir())) == 1
    assert (destination/'usr/libexec/qwq-toss-report/launcher.py').read_bytes() == (args['artifact']/'bootstrap/launcher.py').read_bytes()
    for name in ('tokens','starts','cohorts/'+document['plan_canonical_hash']):
        path = destination/'var/lib/qwq-toss-report'/name
        assert path.stat().st_mode & 0o777 == 0o700
        assert (path,document['service_uid'],document['service_gid']) in owned
    assert not (destination/'etc/systemd').exists()
    assert not (destination/'etc/qwq-toss-report/credentials.env').exists()
