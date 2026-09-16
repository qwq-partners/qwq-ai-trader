"""승인 파싱과 신뢰원 경계. 모든 문서는 합성 입력이다."""
import importlib
import json
import os
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest


def api():
    try:
        return importlib.import_module("src.data.providers.toss.approval")
    except ModuleNotFoundError:
        pytest.fail("승인 구현 없음", pytrace=False)


def plan_document():
    return dict(schema_version=1, plan_id="synthetic-plan", dataset_kind="live",
        origin="https://openapi.tossinvest.com", spec_version="1.2.17",
        spec_sha256="791082da4cb379117ed9fdc29a45bd42746f7a1aec368da1e9f4e1f3bfbff5b4",
        dates=["2026-09-16"], sessions=[dict(name="regular", start="09:00", end="15:30")],
        calendar_time="08:00", selection=dict(candidate_limit=20, max_snapshot_age_seconds=300,
            max_symbols=200, rule="score_desc_symbol_asc_holdings_first"),
        limits=dict(job_timeout_seconds=10, cleanup_timeout_seconds=5, preflight_timeout_seconds=2,
            max_pages=5, max_retries=1, circuit_failure_threshold=3, circuit_open_seconds=60,
            ledger_max_bytes=1000000, response_max_bytes=65536, response_max_depth=20,
            response_max_nodes=10000, response_max_string=16000, parse_timeout_seconds=1,
            auth_max_issues=3, groups=dict(PRICES=3, CANDLES=3, MARKET_INFO=1)),
        comparison=dict(max_age_seconds=60, max_skew_seconds=5, outlier_pct=1,
            min_valid_pairs=10, expected_market_basis="KRX_NXT"),
        acceptance=dict(min_coverage=.8, max_provider_failure_rate=.1, max_p95_pct=1,
            max_outlier_rate=.1, max_missed_slots=1, max_latency_seconds=5,
            retention_days=7, min_business_days=3))


def raw(value):
    return json.dumps(value).encode()


def authority_fixture(tmp_path, monkeypatch, *, role="issuer", capabilities=None):
    mod = api()
    plan = mod.ObservationPlan.from_bytes(raw(plan_document()))
    stamp = [datetime(2026, 9, 16, tzinfo=timezone.utc)]
    ticks = [100.0]
    identity = mod.ExecutionIdentity(client_identity="synthetic-client", host_identity="synthetic-host",
        service_uid=os.geteuid() + 10000, service_gids=(), role=role,
        token_directory=tmp_path / "tokens", sender_lock_path=tmp_path / "sender.lock",
        ledger_path=tmp_path / "ledger.jsonl", release_id="synthetic-release", config_hash="a" * 64)
    grant = dict(schema_version=1, grant_id="synthetic-grant", approval_reference="synthetic-approval",
        terms_reference="synthetic-terms", storage_reference="synthetic-storage",
        issuance_ownership_reference="synthetic-ownership", not_before=stamp[0].isoformat(),
        expires_at=(stamp[0] + timedelta(seconds=60)).isoformat(),
        client_identity=identity.client_identity, host_identity=identity.host_identity,
        service_uid=identity.service_uid, role=role, token_directory=str(identity.token_directory),
        sender_lock_path=str(identity.sender_lock_path), ledger_path=str(identity.ledger_path),
        release_id=identity.release_id, config_hash=identity.config_hash,
        plan_raw_hash=plan.raw_hash, plan_canonical_hash=plan.canonical_hash,
        origin=plan.document["origin"], spec_version="1.2.17", spec_sha256=plan.document["spec_sha256"],
        capabilities=capabilities or dict(query=True, renewal=role == "issuer", bootstrap=role == "issuer"))
    registry = tmp_path / "registry.json"
    registry.write_bytes(raw(dict(schema_version=1, grants=[grant])))
    registry.chmod(0o644)
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(raw(plan_document()))
    # 임시 디렉터리는 실제 /tmp에 있으므로 /tmp의 쓰기 비트만 합성 운영자 경계로 모사한다.
    original = os.fstat
    tmp_inode = os.stat("/tmp").st_ino
    def hardened_tmp(fd):
        metadata = original(fd)
        if metadata.st_ino == tmp_inode and stat.S_ISDIR(metadata.st_mode):
            values = list(metadata)
            values[0] = stat.S_IFDIR | 0o755
            return os.stat_result(values)
        return metadata
    monkeypatch.setattr(os, "fstat", hardened_tmp)
    trust = mod.RegistryTrust(registry_path=registry, operator_uid=os.geteuid(), operator_gid=os.getegid())
    kwargs = dict(registry_path=registry, plan_path=plan_path, grant_id=grant["grant_id"],
        trust=trust, identity=identity, clock=lambda: ticks[0], now=lambda: stamp[0])
    return mod, kwargs, grant, stamp, ticks


def test_plan_is_immutable_and_raw_hash_is_distinct():
    mod = api()
    value = plan_document()
    first = mod.ObservationPlan.from_bytes(raw(value))
    second = mod.ObservationPlan.from_bytes(json.dumps(value, indent=2).encode())
    assert first.raw_hash != second.raw_hash
    assert first.canonical_hash == second.canonical_hash
    with pytest.raises(TypeError):
        first.document["limits"]["max_pages"] = 999
    assert isinstance(first.document["dates"], tuple)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(approved=True), lambda p: p.update(dataset_kind="synthetic"),
    lambda p: p["limits"].update(max_pages=True), lambda p: p["limits"].update(job_timeout_seconds=float("nan")),
    lambda p: p["limits"].update(response_max_depth=1000000),
    lambda p: p["sessions"][0].update(end="08:00"), lambda p: p.update(dates=["2026-02-30"]),
    lambda p: p["acceptance"].update(min_coverage=1.1),
])
def test_plan_rejects_unapproved_or_malformed_policy(mutate):
    mod = api()
    value = plan_document()
    mutate(value)
    with pytest.raises(mod.ApprovalError):
        mod.ObservationPlan.from_bytes(raw(value))


def test_duplicate_json_and_large_input_are_rejected():
    mod = api()
    for payload in (b'{"schema_version":1,"schema_version":1}', b" " * 262145):
        with pytest.raises(mod.ApprovalError):
            mod.ObservationPlan.from_bytes(payload)


def test_load_binds_identity_and_caps_time_even_when_wall_clock_reverses(tmp_path, monkeypatch):
    mod, kwargs, grant, stamp, ticks = authority_fixture(tmp_path, monkeypatch)
    authority = mod.load_authority(**kwargs)
    assert authority.require("query", deadline=1000) == 160
    ticks[0] = 161
    stamp[0] -= timedelta(seconds=1)
    with pytest.raises(mod.ApprovalError):
        authority.require("renewal", deadline=1000)
    assert not kwargs["identity"].token_directory.exists()


@pytest.mark.parametrize("field,value", [("host_identity", "other"), ("release_id", "other"),
    ("config_hash", "b" * 64), ("client_identity", "other"), ("service_uid", 42)])
def test_wrong_execution_binding_is_rejected(tmp_path, monkeypatch, field, value):
    mod, kwargs, *_ = authority_fixture(tmp_path, monkeypatch)
    kwargs["identity"] = replace(kwargs["identity"], **{field: value})
    with pytest.raises(mod.ApprovalError):
        mod.load_authority(**kwargs)


@pytest.mark.parametrize("change", ["writable", "symlink", "hardlink", "untrusted", "self_approved", "parent_writable", "hash"])
def test_registry_cannot_self_approve_or_escape_trust(tmp_path, monkeypatch, change):
    mod, kwargs, grant, *_ = authority_fixture(tmp_path, monkeypatch)
    path = kwargs["registry_path"]
    if change == "writable":
        path.chmod(0o666)
    elif change == "parent_writable":
        path.parent.chmod(0o777)
    elif change == "hardlink":
        os.link(path, tmp_path / "alias")
    elif change == "symlink":
        path.rename(tmp_path / "source")
        path.symlink_to(tmp_path / "source")
    elif change == "untrusted":
        kwargs["trust"] = None
    elif change == "hash":
        kwargs["plan_path"].write_bytes(json.dumps(plan_document(), indent=2).encode())
    else:
        grant["approved"] = True
        path.write_bytes(raw(dict(schema_version=1, grants=[grant])))
    with pytest.raises(mod.ApprovalError):
        mod.load_authority(**kwargs)


def test_reader_stop_and_wall_expiry_block_authorization(tmp_path, monkeypatch):
    mod, kwargs, _, stamp, _ = authority_fixture(tmp_path, monkeypatch, role="reader")
    authority = mod.load_authority(**kwargs)
    assert authority.require("query", deadline=110) == 110
    with pytest.raises(mod.ApprovalError):
        authority.require("bootstrap", deadline=110)
    authority.stop()
    with pytest.raises(mod.ApprovalError):
        authority.require("query", deadline=110)
    authority = mod.load_authority(**kwargs)
    stamp[0] += timedelta(seconds=60)
    with pytest.raises(mod.ApprovalError):
        authority.require("query", deadline=110)


@pytest.mark.parametrize("value", [None, [], {}, True, "1", 1e300, 10**400])
def test_malformed_limit_types_always_raise_safe_approval_error(value):
    mod = api()
    document = plan_document()
    document["limits"]["max_pages"] = value
    with pytest.raises(mod.ApprovalError):
        mod.ObservationPlan.from_bytes(raw(document))


@pytest.mark.parametrize("path", [None, [], 42])
def test_invalid_path_types_are_safe_errors(tmp_path, monkeypatch, path):
    mod, kwargs, *_ = authority_fixture(tmp_path, monkeypatch)
    kwargs["registry_path"] = path
    with pytest.raises(mod.ApprovalError):
        mod.load_authority(**kwargs)
