"""Reader compatibility must retain the sealed LLM-window rejection boundaries."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest


def _sealed_capture(tmp_path):
    from test_capture_readiness import _capture
    from src.analytics.entry_observation_journal import read_observation_journal

    study, journal, _, _ = _capture(tmp_path)
    study_bytes = study.read_bytes()
    study_sha = hashlib.sha256(study_bytes).hexdigest()
    observations = read_observation_journal(
        journal, max_bytes=1048576, expected_study_sha256=study_sha,
    )
    return study_bytes, study_sha, observations


def _build(study_bytes, study_sha, observations):
    from src.analytics.entry_evaluation_bundle import build_evaluation_bundle

    return build_evaluation_bundle(study_bytes, observations, study_sha256=study_sha)


def _renumber(records):
    for sequence, record in enumerate(records, 1):
        record["sequence"] = sequence


def _rebinding_study(study_bytes, observations, mutate):
    context = json.loads(study_bytes)
    mutate(context)
    rebound = json.dumps(context, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()
    study_sha = hashlib.sha256(rebound).hexdigest()
    observations["journal"]["study_sha256"] = study_sha
    return rebound, study_sha


def test_sealed_llm_bundle_rejects_admission_and_record_contract_tampering(tmp_path):
    study_bytes, study_sha, sealed = _sealed_capture(tmp_path)
    starts = [index for index, row in enumerate(sealed["records"])
              if row["kind"] == "llm_check_started"]
    admissions = [index for index, row in enumerate(sealed["records"])
                  if row["kind"] == "llm_check_admission"]
    assert starts and admissions

    missing = deepcopy(sealed)
    missing["records"].pop(admissions[0])
    _renumber(missing["records"])
    with pytest.raises(ValueError, match="LLM admission identity/order mismatch"):
        _build(study_bytes, study_sha, missing)

    wrong_allocation = deepcopy(sealed)
    wrong_allocation["records"][admissions[0]]["allocation_signal_id"] = "foreign"
    with pytest.raises(ValueError, match="LLM admission identity/order mismatch"):
        _build(study_bytes, study_sha, wrong_allocation)

    nonadjacent = deepcopy(sealed)
    start = starts[0]
    nonadjacent["records"].insert(start + 1, {
        "kind": "quote_subscription", "observed_at": nonadjacent["records"][start]["observed_at"],
        "status": "desired", "generation": 0, "connection_id": None,
        "tr_id": "H0STASP0", "symbol": "900001",
    })
    _renumber(nonadjacent["records"])
    with pytest.raises(ValueError, match="LLM admission identity/order mismatch"):
        _build(study_bytes, study_sha, nonadjacent)

    unknown = deepcopy(sealed)
    unknown["records"].append({"kind": "foreign_evidence",
                               "observed_at": unknown["records"][starts[0]]["observed_at"]})
    _renumber(unknown["records"])
    with pytest.raises(ValueError, match="지원하지 않는 관측 kind"):
        _build(study_bytes, study_sha, unknown)

    sequence = deepcopy(sealed)
    sequence["records"][-1]["sequence"] = 999
    with pytest.raises(ValueError, match="선정 관측 순서 불일치"):
        _build(study_bytes, study_sha, sequence)


def test_sealed_llm_bundle_rejects_undeclared_state_old_version_and_hash_mismatch(tmp_path):
    study_bytes, study_sha, sealed = _sealed_capture(tmp_path)

    raw_state = deepcopy(sealed)
    raw_state["records"].append({"kind": "market_state", "observed_at": "2026-10-15T00:00:01+00:00"})
    _renumber(raw_state["records"])
    state_bytes, state_sha = _rebinding_study(
        study_bytes, raw_state,
        lambda context: context["capture"]["channels"].update(market_state_evidence=False),
    )
    with pytest.raises(ValueError, match="market-state records require explicit capture contract"):
        _build(state_bytes, state_sha, raw_state)

    old_version = deepcopy(sealed)
    old_bytes, old_sha = _rebinding_study(
        study_bytes, old_version,
        lambda context: context["capture"].update(
            version="runner-signal-window-v1", quote_admission="signal_created"),
    )
    with pytest.raises(ValueError, match="LLM admission requires explicit study version"):
        _build(old_bytes, old_sha, old_version)

    with pytest.raises(ValueError, match="original study bytes/hash mismatch"):
        _build(study_bytes, "0" * 64, deepcopy(sealed))

    journal_hash = deepcopy(sealed)
    journal_hash["journal"]["study_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="original study/journal binding mismatch"):
        _build(study_bytes, study_sha, journal_hash)
