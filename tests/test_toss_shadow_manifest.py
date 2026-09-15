"""Toss Phase 1 shadow 통계의 순수 오프라인 계약."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.data.providers.toss.shadow import ShadowManifest, build_shadow, summarize_pairs


FIXTURES = Path(__file__).parent / "fixtures" / "toss"


def _manifest_data():
    return json.loads((FIXTURES / "phase1_manifest.json").read_text(encoding="utf-8"))


def _pairs():
    return json.loads((FIXTURES / "phase1_pairs.json").read_text(encoding="utf-8"))


def test_manifest_accepts_explicit_offline_synthetic_nearest_rank_contract():
    manifest = ShadowManifest.from_dict(_manifest_data())

    assert manifest.schema_version == 1
    assert manifest.mode == "offline"
    assert manifest.dataset_kind == "synthetic"
    assert manifest.spec_version == "1.2.17"
    assert manifest.p95_method == "nearest_rank"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "live"),
        ("dataset_kind", "historical"),
        ("spec_sha256", "not-a-sha"),
        ("max_age_seconds", True),
        ("max_pair_skew_seconds", float("inf")),
        ("min_valid_pairs", -1),
        ("min_coverage", 1.01),
        ("p95_limit_pct", -0.01),
        ("outlier_threshold_pct", False),
        ("max_outlier_fraction", float("nan")),
        ("p95_method", "interpolated"),
    ],
)
def test_manifest_rejects_non_offline_or_invalid_threshold_values(field, value):
    # 이 필드를 무검증하거나 live를 허용하도록 바꾸면 실패한다.
    data = _manifest_data()
    data[field] = value

    with pytest.raises(ValueError):
        ShadowManifest.from_dict(data)


def test_default_disabled_shadow_does_not_call_factory():
    # disabled 분기를 제거하거나 factory를 먼저 호출하면 실패한다.
    called = []

    assert build_shadow(factory=lambda: called.append("called")) is None
    assert called == []


@pytest.mark.parametrize("enabled", ["0", "1", 0, 1, None])
def test_shadow_rejects_non_boolean_enable_values_without_truthiness_activation(enabled):
    # 문자열/정수 truthiness를 허용하거나 factory를 호출하면 실패한다.
    called = []

    with pytest.raises(TypeError):
        build_shadow(enabled=enabled, factory=lambda: called.append("called"))

    assert called == []


def test_summary_keeps_attempt_denominator_and_excludes_duplicate_failure_skew_and_stale_rows():
    # 유효 차이: 0.1%, 1.0%; nearest-rank p95 = 두 번째 값 1.0%.
    report = summarize_pairs(_pairs(), ShadowManifest.from_dict(_manifest_data()))

    assert report["attempted_pairs"] == 6
    assert report["valid_pairs"] == 2
    assert report["failed_pairs"] == 1
    assert report["duplicate_pairs"] == 1
    assert report["excluded_reasons"] == {
        "duplicate_symbol_observed_at": 1,
        "provider_status": 1,
        "observation_skew": 1,
        "stale": 1,
    }
    assert report["coverage"] == pytest.approx(2 / 6)
    assert report["p95_difference_pct"] == pytest.approx(1.0)
    assert report["outlier_fraction"] == pytest.approx(0.5)
    assert report["status"] == "insufficient"
    assert report["synthetic_only"] is True
    assert report["production_eligible"] is False
    assert len(report["manifest_sha256"]) == 64
    assert len(report["dataset_sha256"]) == 64
    assert report["hash_input_normalization"] == (
        "documented fields only; sorted-key compact JSON; timestamps normalized to UTC"
    )


def test_dedup_key_uses_both_normalized_provider_observation_times():
    # Toss 관측시각이 달라진 새 쌍을 KIS 시각 하나만으로 중복 제거하면 실패한다.
    first, second = deepcopy(_pairs()[0]), deepcopy(_pairs()[0])
    second["pair_id"] = "p-1-later-toss-observation"
    second["toss"]["observed_at"] = "2026-09-16T00:59:32+00:00"

    report = summarize_pairs([first, second], ShadowManifest.from_dict(_manifest_data()))

    assert report["valid_pairs"] == 2
    assert report["duplicate_pairs"] == 0


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("kis", "price"), True, "invalid_price"),
        (("toss", "price"), float("nan"), "invalid_price"),
        (("kis", "latency_ms"), -1, "invalid_latency"),
        (("toss", "latency_ms"), True, "invalid_latency"),
        (("kis", "observed_at"), "2026-09-16T09:59:30", "invalid_time"),
        (("toss", "fetched_at"), "2026-09-16T09:59:00+09:00", "invalid_time"),
    ],
)
def test_invalid_numeric_or_time_rows_are_attempted_but_not_in_difference_denominator(path, value, reason):
    # 가격/latency/time 검증을 느슨하게 만들어 차이 표본에 넣으면 실패한다.
    row = deepcopy(_pairs()[0])
    row[path[0]][path[1]] = value

    report = summarize_pairs([row], ShadowManifest.from_dict(_manifest_data()))

    assert report["attempted_pairs"] == 1
    assert report["valid_pairs"] == 0
    assert report["excluded_reasons"] == {reason: 1}
    assert report["p95_difference_pct"] is None
    assert report["production_eligible"] is False


def test_summary_only_emits_defined_aggregate_fields_not_raw_provider_content():
    # 출력이 입력 행을 그대로 누출하도록 바뀌면 실패한다.
    report = summarize_pairs(_pairs(), ShadowManifest.from_dict(_manifest_data()))
    serialized = json.dumps(report, allow_nan=False)

    assert "credential=not-for-output" not in serialized
    assert "raw_error" not in serialized
    assert "pair_id" not in serialized


def test_empty_sample_has_no_p95_and_can_never_be_production_eligible():
    report = summarize_pairs([], ShadowManifest.from_dict(_manifest_data()))

    assert report["attempted_pairs"] == 0
    assert report["p95_difference_pct"] is None
    assert report["status"] == "insufficient"
    assert report["production_eligible"] is False
