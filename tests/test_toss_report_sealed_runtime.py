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
import asyncio
import pytest
import sys
from zoneinfo import ZoneInfo

from test_toss_report_launcher import LAUNCHER, approved_release, sealed_release


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
    path = ROOT / 'scripts/ops/toss_report/package_release.py'
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
    plan.update(dates=days, calendar_time='08:55', sessions=[{'name': 'after', 'start': '16:00', 'end': '16:05'}])
    plan['selection'].update(candidate_limit=20, max_symbols=20, max_snapshot_age_seconds=86400)
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



HARNESS = r'''
import importlib.util, json, os, pathlib, socket, stat, sys, time
from datetime import datetime, timezone
base = pathlib.Path(sys.argv[1])
anchor = datetime.fromisoformat(sys.argv[2])
mode = sys.argv[3] if len(sys.argv) > 3 else 'normal'
current = [anchor]
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
    if (name == '/tmp' or name.startswith('/tmp/')) and not (name == str(base / 'state') or name.startswith(str(base / 'state') + '/')):
        row = list(info); row[4] = row[5] = 0
        if stat.S_ISDIR(info.st_mode): row[0] = stat.S_IFDIR | (stat.S_IMODE(info.st_mode) & ~0o022)
        return os.stat_result(row)
    return info
os.fstat = root_metadata
os.getgroups = lambda: [os.getgid()]
# Defense in depth for the isolated test process: any real outbound socket is a test failure.
original_connect = socket.socket.connect
def local_only(sock, address):
    if isinstance(address, tuple): raise AssertionError('network forbidden')
    return original_connect(sock, address)
socket.socket.connect = local_only
original_verifier = launcher.load_verified_document
calls = []
original_fsync = os.fsync
def storage_fault(fd):
    if mode == 'ledger_failure' and 'prices' in calls and '/state/cohorts/' in os.readlink('/proc/self/fd/' + str(fd)):
        raise OSError('synthetic storage failure')
    return original_fsync(fd)
os.fsync = storage_fault

def verified_then_install_http_double(path):
    assert 'src' not in sys.modules
    document = original_verifier(path)
    assert 'src' not in sys.modules
    sys.dont_write_bytecode = True
    root = pathlib.Path(document['release_root'])
    sys.path[:0] = [str(root / 'app'), str(root / 'deps')]
    import aiohttp
    import src.data.providers.toss.approval as approval
    import src.observation.toss_report_helper as service
    real_load = approval.load_authority
    def aligned_load(**kwargs):
        return real_load(**kwargs, now=lambda: current[0].astimezone(timezone.utc), clock=time.monotonic)
    approval.load_authority = aligned_load
    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz) if tz else current[0].replace(tzinfo=None)
    service.datetime = ClockDateTime
    class Response:
        status = 200
        headers = {}
        def __init__(self, value):
            self.raw = json.dumps(value).encode()
            self.content = self
        async def iter_chunked(self, size):
            if mode == 'stall' and 'prices' in calls:
                import asyncio
                await asyncio.Event().wait()
            yield self.raw
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        _retry_connection = True
        def __init__(self, **kwargs):
            if mode == 'cross_session':
                from datetime import timedelta
                current[0] = anchor + timedelta(minutes=5)
        def request(self, method, url, **kwargs):
            if method == 'POST' and url == 'https://openapi.tossinvest.com/oauth2/token':
                calls.append('token')
                assert kwargs['data']['client_id'] == 'synthetic-client'
                return Response({'access_token':'synthetic-token', 'token_type':'Bearer', 'expires_in':3600})
            assert method == 'GET' and url == 'https://openapi.tossinvest.com/api/v1/prices'
            assert kwargs['params'] == {'symbols':'087010,005930'}
            calls.append('prices')
            (base/'http_started').write_text('synthetic')
            return Response({'result':[{'symbol':symbol,'lastPrice':'100','currency':'KRW','timestamp':anchor.isoformat()}
                                       for symbol in ('087010','005930')]})
        async def close(self): pass
    aiohttp.ClientSession = Session
    return document
launcher.load_verified_document = verified_then_install_http_double
sys.argv = ['launcher.py']
code = launcher.main()
document = json.loads((base / 'etc/deployment.json').read_text())
ledger = pathlib.Path(document['ledger_path'])
rows = [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
trading = [name for name in sys.modules if name == 'scripts.run_trader' or name.startswith(('src.core', 'src.brokers', 'src.trading'))]
(base / 'result.json').write_text(json.dumps({'code':code,'calls':calls,'starts':len(list((base/'state/starts').iterdir())),
    'types':[row['type'] for row in rows], 'trading':trading}))
'''

@pytest.mark.asyncio
@pytest.mark.parametrize('duplicate', [False, True])
async def test_sealed_report_real_authority_worker_http_parser_ledger_and_ipc(approved_release, tmp_path, duplicate):
    from src.data.providers.toss.report_protocol import ReportCohort, make_request, canonical_bytes, validate_result
    module, document, plan, grant = approved_release
    anchor = datetime(2026, 10, 26, 16, tzinfo=ZoneInfo('Asia/Seoul'))
    _seal_runtime_artifact(module, document, grant, plan, anchor=anchor)
    cohort = ReportCohort.create(anchor.date(), anchor.replace(hour=8, minute=25), ((1, '087010'), (2, '005930')))
    request = make_request(cohort, request_id='a'*32, now=anchor)
    child = await asyncio.create_subprocess_exec(sys.executable, '-I', '-S', '-c', HARNESS,
        str(tmp_path), anchor.isoformat(), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={'TOSS_API':'1', 'TOSS_CLIENT_ID':'synthetic-client', 'TOSS_CLIENT_SECRET':'synthetic-secret',
             'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}, cwd=tmp_path)
    try:
        child.stdin.write(canonical_bytes(request) + b'\n')
        await child.stdin.drain()
        response = await asyncio.wait_for(child.stdout.readline(), 15)
        assert response, (await child.stderr.read()).decode()
        quotes, expiry = validate_result(json.loads(response), request=request, now=anchor)
        assert list(quotes) == ['087010', '005930']
        assert str(quotes['087010'].price) == '100'
        assert quotes['087010'].market_basis == 'unknown'
        assert expiry == anchor + timedelta(seconds=60)
        if duplicate:
            child.stdin.write(canonical_bytes(request) + b'\n')
            await child.stdin.drain()
        else:
            child.stdin.close()
        await asyncio.wait_for(child.wait(), 10)
        assert await child.stdout.read() == b''
        assert await child.stderr.read() == b''
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    outcome = json.loads((tmp_path/'result.json').read_text())
    assert outcome['code'] == int(duplicate)
    assert outcome['calls'] == ['token', 'prices']
    assert outcome['starts'] == 1
    assert outcome['types'].count('terminal') == 2
    assert outcome['trading'] == []

@pytest.mark.asyncio
async def test_startup_cannot_issue_token_after_report_session_ends(approved_release, tmp_path):
    from src.data.providers.toss.report_protocol import ReportCohort, make_request, canonical_bytes
    module, document, plan, grant = approved_release
    anchor = datetime(2026, 10, 26, 16, tzinfo=ZoneInfo('Asia/Seoul'))
    _seal_runtime_artifact(module, document, grant, plan, anchor=anchor)
    cohort = ReportCohort.create(anchor.date(), anchor.replace(hour=8), ((1,'087010'),(2,'005930')))
    request = make_request(cohort, request_id='b'*32, now=anchor)
    child = await asyncio.create_subprocess_exec(sys.executable,'-I','-S','-c',HARNESS,str(tmp_path),
        anchor.isoformat(),'cross_session', stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={'TOSS_API':'1','TOSS_CLIENT_ID':'synthetic-client','TOSS_CLIENT_SECRET':'synthetic-secret'},cwd=tmp_path)
    try:
        child.stdin.write(canonical_bytes(request)+b'\n')
        await child.stdin.drain()
        await asyncio.wait_for(child.wait(),15)
        assert await child.stdout.read() == b''
    finally:
        if child.returncode is None: child.kill(); await child.wait()
    outcome = json.loads((tmp_path/'result.json').read_text())
    assert outcome['code'] == 1
    assert outcome['calls'] == []

@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['ledger_failure', 'stall'])
async def test_failed_ledger_or_parent_eof_never_publishes_prices(approved_release, tmp_path, mode):
    from src.data.providers.toss.report_protocol import ReportCohort, make_request, canonical_bytes
    module, document, plan, grant = approved_release
    anchor = datetime(2026,10,26,16,tzinfo=ZoneInfo('Asia/Seoul'))
    _seal_runtime_artifact(module,document,grant,plan,anchor=anchor)
    cohort = ReportCohort.create(anchor.date(),anchor.replace(hour=8),((1,'087010'),(2,'005930')))
    request = make_request(cohort,request_id='c'*32,now=anchor)
    child = await asyncio.create_subprocess_exec(sys.executable,'-I','-S','-c',HARNESS,str(tmp_path),
        anchor.isoformat(),mode,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
        env={'TOSS_API':'1','TOSS_CLIENT_ID':'synthetic-client','TOSS_CLIENT_SECRET':'synthetic-secret'},cwd=tmp_path)
    try:
        child.stdin.write(canonical_bytes(request)+b'\n')
        await child.stdin.drain()
        if mode == 'stall':
            async with asyncio.timeout(5):
                while not (tmp_path/'http_started').exists(): await asyncio.sleep(.01)
            child.stdin.close()
            await asyncio.wait_for(child.wait(),10)
            assert await child.stdout.read() == b''
        else:
            response = json.loads(await asyncio.wait_for(child.stdout.readline(),10))
            assert response['status'] == 'unavailable'
            assert response['quotes'] == [] and response['valid_until'] is None
            child.stdin.close()
            await asyncio.wait_for(child.wait(),10)
        assert await child.stderr.read() == b''
    finally:
        if child.returncode is None: child.kill(); await child.wait()
    outcome = json.loads((tmp_path/'result.json').read_text())
    assert outcome['calls'] == ['token','prices']
    assert outcome['starts'] == 1
    assert outcome['trading'] == []
    if mode == 'stall': assert outcome['code'] == 1


@pytest.mark.asyncio
async def test_actual_parent_and_evening_report_consume_only_sealed_cohort_prices(approved_release, tmp_path, monkeypatch):
    from src.analytics import daily_report as dr
    from src.data.providers.toss import report_helper
    module, document, plan, grant = approved_release
    anchor = datetime(2026, 10, 26, 16, tzinfo=ZoneInfo('Asia/Seoul'))
    _seal_runtime_artifact(module, document, grant, plan, anchor=anchor)
    monkeypatch.setenv('TOSS_REPORT_API', '1')
    monkeypatch.setenv('TOSS_CLIENT_ID', 'synthetic-client')
    monkeypatch.setenv('TOSS_CLIENT_SECRET', 'synthetic-secret')
    monkeypatch.setattr(report_helper, '_ARGV', (sys.executable, '-I', '-S', '-c', HARNESS,
                                               str(tmp_path), anchor.isoformat()))
    monkeypatch.setattr(report_helper, '_now', lambda: anchor)
    monkeypatch.setattr(dr, 'date', type('FixedDate', (), {'today': staticmethod(lambda: anchor.date())}))
    monkeypatch.setattr(dr, '_REC_CACHE_DIR', tmp_path / 'recommendations')
    generator = dr.DailyReportGenerator.__new__(dr.DailyReportGenerator)
    generator._today_recommendations = [dr.RecommendedStock(rank=rank, symbol=symbol,
        name=symbol, investment_thesis='', catalyst='', reference_price=90)
        for rank, symbol in ((1, '087010'), (2, '005930'))]
    generator._recommendation_date = anchor.date()
    generator._report_cohort = None
    generator._set_report_cohort(anchor.date(), anchor.replace(hour=8, minute=25))
    generator._save_recommendations(anchor.date())
    generator._today_recommendations = []  # 실제 저녁 복원 경로도 통과한다.
    generator._report_cohort = None
    client = report_helper.ReportHelperClient(enabled=lambda: True)
    generator.toss_report_prepare = client.prepare_report
    def current_quote(symbol):
        cohort = generator._valid_report_cohort(anchor.date())
        return None if cohort is None else client.get_report_quote(symbol, cohort=cohort)
    generator.toss_quote_provider = current_quote
    try:
        report = await generator.generate_evening_report(send_telegram=False)
        assert [stock.symbol for stock in generator._today_recommendations] == ['087010', '005930']
        assert all(stock.result_price == 100 for stock in generator._today_recommendations)
        assert all(stock.result_pct is None for stock in generator._today_recommendations)
        assert all(stock.result_source == 'Toss 현재가 (시장 기준 미확인)' for stock in generator._today_recommendations)
        assert '가격만 관측 2' in report
        assert generator._report_cohort.generated_at == anchor.replace(hour=8, minute=25)
    finally:
        await client.close()
    result = json.loads((tmp_path / 'result.json').read_text())
    assert result['code'] == 0 and result['starts'] == 1
    assert result['calls'] == ['token', 'prices']
    assert result['types'].count('terminal') == 2
    assert result['trading'] == []
    assert client.get_report_quote('087010') is None
