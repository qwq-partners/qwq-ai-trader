"""검증된 launcher 입력을 기존 승인 객체와 단일 시작 영수증으로 조립한다."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat

from src.data.providers.toss.approval import ApprovalError, ExecutionIdentity, RegistryTrust
from src.data.providers.toss.runtime_factory import Deployment, StartupAttestation


def _validate_policy(authority, document):
    """넓은 기존 plan 스키마 안에서 승인한 첫 관측 정책만 허용한다."""
    plan, grant = authority.plan, authority.grant
    if (plan.raw_hash != document['plan_raw_hash'] or plan.canonical_hash != document['plan_canonical_hash']
            or grant.role != 'issuer' or dict(grant.capabilities) != dict(query=True, renewal=True, bootstrap=True)):
        raise ApprovalError()
    p = plan.document
    expected = {
        'selection': dict(candidate_limit=0, max_snapshot_age_seconds=300, max_symbols=20,
                          rule='score_desc_symbol_asc_holdings_first'),
        'comparison': dict(max_age_seconds=60, max_skew_seconds=5, outlier_pct=.5,
                           min_valid_pairs=100, expected_market_basis='unknown'),
        'acceptance': dict(min_coverage=.95, max_provider_failure_rate=.05, max_p95_pct=.5,
            max_outlier_rate=.05, max_missed_slots=11, max_latency_seconds=20,
            retention_days=30, min_business_days=3),
        'limits': dict(job_timeout_seconds=20, cleanup_timeout_seconds=10, preflight_timeout_seconds=5,
            max_pages=2, max_retries=1, circuit_failure_threshold=3, circuit_open_seconds=300,
            ledger_max_bytes=16777216, response_max_bytes=262144, response_max_depth=8,
            response_max_nodes=10000, response_max_string=16384, parse_timeout_seconds=.2,
            auth_max_issues=4, groups=dict(PRICES=1, CANDLES=1, MARKET_INFO=1)),
    }
    for key, value in expected.items():
        if p[key] != value:
            raise ApprovalError()
    if (tuple(dict(s) for s in p['sessions']) != (dict(name='regular', start='09:00', end='15:20'),)
            or p['calendar_time'] != '08:55' or len(p['dates']) != 3):
        raise ApprovalError()
    start, end = datetime.fromisoformat(grant.not_before), datetime.fromisoformat(grant.expires_at)
    retention = datetime.fromisoformat(document['retention_at'])
    if end - start > timedelta(days=7) or retention != end + timedelta(days=30):
        raise ApprovalError()


def make_deployment(document: dict) -> Deployment:
    identity = ExecutionIdentity(client_identity=document['client_identity'],
        host_identity=document['host_identity'], service_uid=document['service_uid'],
        service_gids=tuple(document['service_gids']), role='issuer',
        token_directory=Path(document['token_directory']), sender_lock_path=Path(document['sender_lock_path']),
        ledger_path=Path(document['ledger_path']), release_id=document['release_id'], config_hash=document['config_hash'])
    deployment = Deployment(registry_path=Path(document['registry_path']), plan_path=Path(document['plan_path']),
        grant_id=document['grant_id'], trust=RegistryTrust(Path(document['registry_path']), 0, 0),
        identity=identity, preflight_timeout_seconds=5,
        startup_attestation=StartupAttestation(document['release_id'], document['config_hash'],
            document['artifact_sha256'], document['host_identity'], document['service_uid']),
        expected_artifact_sha256=document['artifact_sha256'])
    authority = deployment.load()
    _validate_policy(authority, document)
    return deployment


def _private_directory(path, document):
    """root 부모 뒤의 state/starts만 서비스 UID 0700으로 허용한다."""
    path, state = Path(path), Path(document['state_directory'])
    if not path.is_absolute() or '..' in path.parts or path != state / 'starts':
        raise ApprovalError('approval_untrusted')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        current = Path('/')
        for part in path.parts[1:]:
            current /= part
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if current in (state, path):
                valid = (info.st_uid == document['service_uid'] and info.st_gid == document['service_gid']
                         and stat.S_IMODE(info.st_mode) == 0o700)
            else:
                valid = info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o022
            if not valid:
                raise ApprovalError('approval_untrusted')
        return fd
    except BaseException:
        os.close(fd)
        raise


def claim_start(document: dict, authority) -> None:
    """grant별 최초 intent를 영속화한다. 실패한 파일도 절대 삭제하지 않는다."""
    authority.require('query', deadline=authority.clock() + 5)
    grant = authority.grant
    for field in ('grant_id', 'release_id', 'config_hash', 'client_identity', 'host_identity',
                  'service_uid', 'token_directory', 'sender_lock_path', 'ledger_path'):
        if document[field] != getattr(grant, field):
            raise ApprovalError('approval_denied')
    if (document['plan_raw_hash'] != authority.plan.raw_hash
            or document['plan_canonical_hash'] != authority.plan.canonical_hash
            or os.getuid() != document['service_uid'] or os.geteuid() != document['service_uid']
            or os.getgid() != document['service_gid'] or os.getegid() != document['service_gid']):
        raise ApprovalError('approval_denied')
    directory_fd = receipt_fd = None
    try:
        directory_fd = _private_directory(document['receipt_directory'], document)
        # grant ID 원문을 파일명에 넣지 않아 경로 탈출/별칭을 차단한다.
        name = hashlib.sha256(grant.grant_id.encode('utf-8')).hexdigest() + '.json'
        receipt_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory_fd)
        info = os.fstat(receipt_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != document['service_uid']
                or info.st_gid != document['service_gid'] or stat.S_IMODE(info.st_mode) != 0o600):
            raise ApprovalError('approval_untrusted')
        value = dict(schema_version=1, grant_id=grant.grant_id, release_id=document['release_id'],
            config_hash=document['config_hash'], artifact_sha256=document['artifact_sha256'],
            authority_hash=authority.authority_hash, pid=os.getpid(),
            claimed_at=datetime.now(timezone.utc).isoformat())
        payload = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')
        while payload:
            written = os.write(receipt_fd, payload)
            if written <= 0:
                raise OSError('receipt_write_failed')
            payload = payload[written:]
        os.fsync(receipt_fd)
        os.fsync(directory_fd)
        authority.require('query', deadline=authority.clock() + 5)
    except OSError:
        raise ApprovalError('approval_denied') from None
    finally:
        if receipt_fd is not None:
            os.close(receipt_fd)
        if directory_fd is not None:
            os.close(directory_fd)
