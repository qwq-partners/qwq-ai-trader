"""Narrow report-only authority assembled only after sealed launcher verification."""
from datetime import datetime, timedelta
from pathlib import Path

from src.data.providers.toss.approval import ApprovalError, ExecutionIdentity, RegistryTrust
from src.data.providers.toss.runtime_factory import Deployment, StartupAttestation


def validate_report_policy(authority, document):
    plan, grant = authority.plan, authority.grant
    p = plan.document
    if (p['schema_version'] != 3 or grant.schema_version != 3 or grant.role != 'issuer'
            or plan.raw_hash != document['plan_raw_hash']
            or plan.canonical_hash != document['plan_canonical_hash']
            or dict(grant.capabilities) != dict(query=True, renewal=True, bootstrap=True, report_display=True)
            or dict(p['report_display']) != dict(purpose='daily_report_current_price', max_age_seconds=60)
            or tuple(dict(s) for s in p['sessions']) != (dict(name='after', start='16:00', end='16:05'),)
            or not 1 <= len(p['dates']) <= 7):
        raise ApprovalError()
    expected = {
        'selection': dict(candidate_limit=20, max_snapshot_age_seconds=86400, max_symbols=20,
                          rule='score_desc_symbol_asc_holdings_first'),
        'comparison': dict(max_age_seconds=60, max_skew_seconds=5, outlier_pct=.5,
                           min_valid_pairs=100, expected_market_basis='unknown'),
        'limits': dict(job_timeout_seconds=20, cleanup_timeout_seconds=10, preflight_timeout_seconds=5,
            max_pages=2, max_retries=1, circuit_failure_threshold=3, circuit_open_seconds=300,
            ledger_max_bytes=16777216, response_max_bytes=262144, response_max_depth=8,
            response_max_nodes=10000, response_max_string=16384, parse_timeout_seconds=.2,
            auth_max_issues=4, groups=dict(PRICES=1, CANDLES=1, MARKET_INFO=1)),
    }
    if any(p[key] != value for key, value in expected.items()):
        raise ApprovalError()
    start, end = datetime.fromisoformat(grant.not_before), datetime.fromisoformat(grant.expires_at)
    if (not timedelta(0) < end - start <= timedelta(days=7)
            or datetime.fromisoformat(document['retention_at']) != end + timedelta(days=30)):
        raise ApprovalError()


def make_report_deployment(document):
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
        expected_artifact_sha256=document['artifact_sha256'], report_display=True)
    validate_report_policy(deployment.load(), document)
    return deployment
