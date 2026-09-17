"""Sealed ``-I -S`` launcher acceptance test with synthetic worker I/O only.

The subprocess has a clean environment and copies the application closure plus
the test interpreter's installed aiohttp closure into a temporary, manifest-sealed release.  It does
not access an operator credential, a real endpoint, or an installed release.
The harness fakes only root ownership metadata and the HTTP/token edge; normal
``main() -> make_deployment() -> run_service()`` still owns the real worker,
``ObservationApp``, durable ledger, receipt, and signal-driven shutdown.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from zoneinfo import ZoneInfo

from test_toss_observer_launcher import LAUNCHER, approved_release, sealed_release


ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = (
    'aiohttp', 'aiohappyeyeballs', 'aiosignal', 'attrs', 'frozenlist',
    'multidict', 'propcache', 'yarl', 'idna', 'typing_extensions',
)
DEPENDENCY_PATHS = {
    'aiohttp': ('aiohttp',),
    'aiohappyeyeballs': ('aiohappyeyeballs',),
    'aiosignal': ('aiosignal',),
    'attrs': ('attr', 'attrs'),
    'frozenlist': ('frozenlist',),
    'multidict': ('multidict',),
    'propcache': ('propcache',),
    'yarl': ('yarl',),
    'idna': ('idna',),
    'typing_extensions': ('typing_extensions.py',),
}


def _load_packager():
    path = ROOT / 'scripts/ops/toss_observer/package_release.py'
    spec = importlib.util.spec_from_file_location('sealed_runtime_packager', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_without_links(source: Path, destination: Path) -> None:
    """Copy a package tree as regular artifact files, never following links."""
    if source.is_symlink():
        raise AssertionError(f'symlinked test dependency: {source.name}')
    if source.is_dir():
        for entry in source.rglob('*'):
            if entry.is_symlink():
                raise AssertionError(f'symlinked test dependency: {entry.name}')
        shutil.copytree(source, destination,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _copy_installed_dependencies(destination: Path) -> None:
    """Copy the installed dependency closure; this is not a lockfile check."""
    aiohttp = importlib.util.find_spec('aiohttp')
    assert aiohttp is not None and aiohttp.origin is not None
    site_packages = Path(aiohttp.origin).resolve().parents[1]
    for distribution in DEPENDENCIES:
        matches = sorted(site_packages.glob(distribution.replace('_', '[-_]', 1) + '-*.dist-info'))
        # ``attrs`` has the import package ``attr`` and the distribution attrs.
        if distribution == 'attrs':
            matches = sorted(site_packages.glob('attrs-*.dist-info'))
        assert len(matches) == 1, distribution
        _copy_without_links(matches[0], destination / matches[0].name)
        for relative in DEPENDENCY_PATHS[distribution]:
            source = site_packages / relative
            assert source.exists(), relative
            _copy_without_links(source, destination / relative)


def _seal_runtime_artifact(module, document, grant, plan, *, anchor: datetime) -> None:
    """Build a self-contained temporary artifact after all test-only files exist."""
    release = Path(document['release_root'])
    packager = _load_packager()
    for source in packager.source_files(ROOT):
        target = release / 'app' / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    _copy_installed_dependencies(release / 'deps')

    # The real deployment policy permits exactly this narrow, synthetic window.
    days = [(anchor.date() + timedelta(days=offset)).isoformat() for offset in range(3)]
    plan.update(dates=days, calendar_time='08:55', sessions=[{'name': 'regular', 'start': '09:00', 'end': '15:20'}])
    plan['selection'].update(candidate_limit=0, max_symbols=20, max_snapshot_age_seconds=300)
    plan['limits'].update(job_timeout_seconds=20, cleanup_timeout_seconds=10,
                          preflight_timeout_seconds=5, max_pages=2, max_retries=1,
                          circuit_failure_threshold=3, circuit_open_seconds=300,
                          ledger_max_bytes=16777216, response_max_bytes=262144,
                          response_max_depth=8, response_max_nodes=10000,
                          response_max_string=16384, parse_timeout_seconds=.2,
                          auth_max_issues=4, groups={'PRICES': 1, 'CANDLES': 1, 'MARKET_INFO': 1})
    plan['comparison'].update(max_age_seconds=60, max_skew_seconds=5, outlier_pct=.5,
                              min_valid_pairs=100, expected_market_basis='unknown')
    plan['acceptance'].update(min_coverage=.95, max_provider_failure_rate=.05,
                              max_p95_pct=.5, max_outlier_rate=.05, max_missed_slots=11,
                              max_latency_seconds=20, retention_days=30, min_business_days=3)
    raw_plan = json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()
    document['plan_raw_hash'] = hashlib.sha256(raw_plan).hexdigest()
    document['plan_canonical_hash'] = hashlib.sha256(module.canonical_bytes(plan)).hexdigest()
    state = Path(document['state_directory'])
    state.chmod(0o700)
    (state / 'starts').mkdir(mode=0o700)
    (state / 'cohorts').mkdir(mode=0o755)
    cohort = state / 'cohorts' / document['plan_canonical_hash']
    cohort.mkdir(mode=0o700)
    document.update(ledger_path=str(cohort / 'observations.jsonl'), status_path=str(cohort / 'status.json'),
                    retention_at=(anchor.astimezone(timezone.utc) + timedelta(hours=1, days=30)).isoformat())
    grant.update(plan_raw_hash=document['plan_raw_hash'], plan_canonical_hash=document['plan_canonical_hash'],
                 ledger_path=document['ledger_path'], not_before=(anchor.astimezone(timezone.utc) - timedelta(minutes=1)).isoformat(),
                 expires_at=(anchor.astimezone(timezone.utc) + timedelta(hours=1)).isoformat())

    for path in release.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)
    manifest = module.build_manifest(release, release_id=document['release_id'],
                                     python_version='.'.join(map(str, sys.version_info[:3])))
    raw_manifest = module.canonical_bytes(manifest)
    digest = hashlib.sha256(raw_manifest).hexdigest()
    (release / 'manifest.json').write_bytes(raw_manifest)
    release.rename(release.parent / digest)
    release = release.parent / digest
    document.update(artifact_sha256=digest, release_root=str(release),
                    manifest_path=str(release / 'manifest.json'))
    document['config_hash'] = module.configuration_hash(document)
    grant['config_hash'] = document['config_hash']
    Path(document['plan_path']).write_bytes(raw_plan)
    Path(document['registry_path']).write_bytes(module.canonical_bytes({'schema_version': 1, 'grants': [grant]}))
    (module.CONFIG_DIRECTORY / 'deployment.json').write_bytes(module.canonical_bytes(document))
    for path in (Path(document['plan_path']), Path(document['registry_path']), module.CONFIG_DIRECTORY / 'deployment.json'):
        path.chmod(0o644)


def test_sealed_isolated_main_runs_real_service_worker_and_ledger(approved_release, tmp_path):
    """A broken launcher/service handoff, worker, ledger, or cleanup must fail here."""
    module, document, plan, grant = approved_release
    anchor = datetime(2026, 9, 18, 9, tzinfo=ZoneInfo('Asia/Seoul'))
    _seal_runtime_artifact(module, document, grant, plan, anchor=anchor)
    release = Path(document['release_root'])
    manifest_files = {entry['path'] for entry in json.loads((release / 'manifest.json').read_bytes())['files']}
    for distribution in DEPENDENCIES:
        assert any(path.startswith('deps/' + distribution + '-') and path.endswith('.dist-info/METADATA')
                   for path in manifest_files), distribution
        assert any(path == 'deps/' + relative or path.startswith('deps/' + relative + '/')
                   for relative in DEPENDENCY_PATHS[distribution] for path in manifest_files), distribution

    # Test-only control plane.  The launcher's normal mode is used after this
    # setup; no production CLI option or import path escape exists.
    harness = r'''
import asyncio, importlib.util, json, os, pathlib, signal, stat, sys, time
from datetime import datetime, timezone
base = pathlib.Path(sys.argv[1])
anchor = datetime.fromisoformat(sys.argv[2])
spec = importlib.util.spec_from_file_location('sealed_launcher', base / 'launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
launcher.CONFIG_DIRECTORY = base / 'etc'
launcher.RELEASES_DIRECTORY = base / 'releases'
launcher.STATE_DIRECTORY = base / 'state'
launcher.TRUSTED_LAUNCHER = base / 'launcher.py'
original_fstat = os.fstat
def root_metadata(fd):
    info = original_fstat(fd)
    name = os.readlink('/proc/self/fd/' + str(fd))
    # Only the root-owned install/config/release side is synthetic.  Runtime
    # state keeps its real test UID so receipt/status/ledger checks remain real.
    if (name == '/tmp' or name.startswith('/tmp/')) and not (name == str(base / 'state') or name.startswith(str(base / 'state') + '/')):
        row = list(info); row[4] = row[5] = 0
        if stat.S_ISDIR(info.st_mode): row[0] = stat.S_IFDIR | (stat.S_IMODE(info.st_mode) & ~0o022)
        return os.stat_result(row)
    return info
os.fstat = root_metadata
os.getgroups = lambda: [os.getgid()]
original_verifier = launcher.load_verified_document
def verified_then_install_doubles(path):
    assert 'src' not in sys.modules
    document = original_verifier(path)
    # The sealed verifier completed before any application source was available.
    assert 'src' not in sys.modules and 'scripts.run_trader' not in sys.modules
    sys.dont_write_bytecode = True
    root = pathlib.Path(document['release_root'])
    sys.path[:0] = [str(root / 'app'), str(root / 'deps')]
    import src.data.providers.toss.approval as approval
    import src.observation.toss_service as service
    from src.data.providers.toss.observation import ObservationRunner
    from src.data.providers.toss.observation_ledger import ObservationLedger
    from src.data.providers.toss.runtime_factory import ObservationApp
    real_load = approval.load_authority
    def aligned_load(**kwargs):
        return real_load(**kwargs, clock=time.monotonic, now=lambda: anchor.astimezone(timezone.utc))
    approval.load_authority = aligned_load
    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return anchor.astimezone(tz) if tz else anchor.replace(tzinfo=None)
    service.datetime = ClockDateTime
    class Positions:
        async def fetch(self): return ('087010',)
        async def close(self): pass
    class Client:
        async def start(self): pass
        async def close(self): pass
        async def get(self, path, *, params, budget):
            if path == '/api/v1/prices' and params == {'symbols': '087010'}:
                return {'result': [{'symbol': '087010', 'lastPrice': '100', 'currency': 'KRW', 'timestamp': anchor.isoformat()}]}
            if path == '/api/v1/market-calendar/KR' and params == {'date': anchor.date().isoformat()}:
                def day(value):
                    return {'date': value, 'integrated': {'preMarket': None, 'regularMarket': {'startTime': value + 'T09:00:00+09:00', 'endTime': value + 'T15:20:00+09:00'}, 'afterMarket': None}}
                from datetime import timedelta
                return {'result': {'today': day(anchor.date().isoformat()), 'previousBusinessDay': day((anchor - timedelta(days=1)).date().isoformat()), 'nextBusinessDay': day((anchor + timedelta(days=1)).date().isoformat())}}
            raise AssertionError('unexpected synthetic request')
    async def cached_token(self, *, deadline): return 'synthetic-test-token'
    def app_factory(authority, stopping):
        policy = authority.plan.document
        client = Client()
        ledger = ObservationLedger(authority.grant.ledger_path, plan_hash=authority.plan.canonical_hash, max_bytes=policy['limits']['ledger_max_bytes'])
        runner = ObservationRunner(client=client, ledger=ledger, policy=policy, clock=authority.clock, now=authority.now)
        return ObservationApp(authority, stopping, client, None, type('Tokens', (), {'get_token': cached_token})(), ledger, runner)
    service.PositionsClient = Positions
    service.build_app = app_factory
    real_write = service.StatusWriter.write
    def stop_after_price(self, status):
        real_write(self, status)
        if status['by_kind']['prices']['observations'] == 1 and status['last_ledger_complete_at']:
            os.kill(os.getpid(), signal.SIGTERM)
    service.StatusWriter.write = stop_after_price
    return document
launcher.load_verified_document = verified_then_install_doubles
sys.argv = ['launcher.py']
code = launcher.main()
status_path = pathlib.Path(json.loads((base / 'etc/deployment.json').read_text())['status_path'])
status = json.loads(status_path.read_text())
rows = [json.loads(line) for line in pathlib.Path(json.loads((base / 'etc/deployment.json').read_text())['ledger_path']).read_text().splitlines()]
starts = list((base / 'state/starts').iterdir())
tokens = pathlib.Path(json.loads((base / 'etc/deployment.json').read_text())['token_directory'])
trading_modules = [name for name in sys.modules if name == 'scripts.run_trader' or name.startswith(('src.brokers', 'src.core', 'src.trading'))]
(base / 'result.json').write_text(json.dumps({'code': code, 'status': status, 'types': [row['type'] for row in rows], 'starts': len(starts), 'tokens': tokens.exists() and list(tokens.iterdir()), 'trading_modules': trading_modules}))
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-c', harness, str(tmp_path), anchor.isoformat()],
                            env={'TOSS_API': '1', 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ''
    outcome = json.loads((tmp_path / 'result.json').read_text())
    assert outcome['code'] == 0
    assert outcome['status']['state'] == 'closed'
    assert outcome['status']['accounting_consistent'] is True
    assert outcome['status']['by_kind']['prices']['observations'] == 1
    assert outcome['status']['last_ledger_complete_at'] is not None
    assert outcome['status']['production_eligible'] is False
    assert outcome['types'].count('terminal') >= 2
    assert outcome['starts'] == 1
    assert outcome['tokens'] is False
    assert outcome['trading_modules'] == []
