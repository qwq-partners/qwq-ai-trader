"""Report authority cannot inherit observer capabilities or schedule."""
from pathlib import Path
import importlib
import pytest
from test_toss_report_launcher import approved_release, sealed_release

def api():
    assert Path('src/observation/toss_report_deployment.py').exists(), 'report deployment missing'
    return importlib.import_module('src.observation.toss_report_deployment')

def test_report_deployment_loads_v3_and_claims_only_explicit_start(approved_release):
    launcher, document, _, _ = approved_release
    deployment = api().make_report_deployment(document)
    assert deployment.report_display is True
    assert deployment.load().grant.capabilities['report_display'] is True
    assert list(Path(document['state_directory']).iterdir()) == []

@pytest.mark.parametrize('change', ['version', 'capability', 'session', 'symbols', 'ttl', 'timeout'])
def test_report_deployment_rejects_broader_policy(approved_release, change):
    launcher, document, plan, grant = approved_release
    if change == 'version':
        plan['schema_version'] = grant['schema_version'] = 1
        del plan['report_display']
        del grant['capabilities']['report_display']
    elif change == 'capability': grant['capabilities']['report_display'] = False
    elif change == 'session': plan['sessions'][0]['start'] = '15:55'
    elif change == 'symbols': plan['selection']['max_symbols'] = 21
    elif change == 'ttl': plan['report_display']['max_age_seconds'] = 61
    else: plan['limits']['job_timeout_seconds'] = 21
    import hashlib
    raw = launcher.canonical_bytes(plan)
    document['plan_raw_hash'] = document['plan_canonical_hash'] = hashlib.sha256(raw).hexdigest()
    document['config_hash'] = launcher.configuration_hash(document)
    for key in ('plan_raw_hash', 'plan_canonical_hash', 'config_hash'): grant[key] = document[key]
    Path(document['plan_path']).write_bytes(raw)
    Path(document['registry_path']).write_bytes(launcher.canonical_bytes(dict(schema_version=1, grants=[grant])))
    with pytest.raises(Exception): api().make_report_deployment(document)
