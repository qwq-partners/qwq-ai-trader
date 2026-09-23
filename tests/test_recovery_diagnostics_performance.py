"""Test-only contracts for the opt-in recovery diagnostics scale harness."""

import asyncio
import json
import os

import pytest

from recovery_scale_harness import SIZES, classify_measurement, measure_case


def test_report_distinguishes_unavailable_from_fast_success():
    row = classify_measurement(snapshot_stable=None, wall_max_ms=1.0)

    assert row['capture_supported'] is False
    assert row['connection_eligible'] is False


def test_fast_stable_measurement_is_connection_eligible():
    row = classify_measurement(snapshot_stable=True, wall_max_ms=50.0)

    assert row == {'capture_supported': True, 'connection_eligible': True}


@pytest.mark.parametrize(('kind', 'sweep_phase'), [
    ('capture', None), ('owner', None), ('sweep', 'cold'), ('sweep', 'warm'),
], ids=['capture', 'owner', 'sweep-cold', 'sweep-warm'])
def test_measure_case_reports_finite_nonidentifying_schema(tmp_path, kind, sweep_phase):
    row = asyncio.run(measure_case(0, kind, tmp_path=tmp_path, sweep_phase=sweep_phase))

    assert row['status'] == 'measured'
    assert row['synthetic'] is True
    assert row['kind'] == kind
    assert row['sweep_phase'] == sweep_phase
    assert row['record_counts'] == {'intents': 0, 'attempts': 0, 'outbox': 0}
    assert row['rows_linked'] is True
    assert row['unique_rows'] is True
    assert row['terminal_reservations'] == 0
    assert row['synthetic_finality'] == 'none'
    assert row['report_bytes'] > 0
    assert row['peak_bytes'] >= 0
    assert row['wall_max_ms'] >= 0
    assert row['loop_stall_ms'] >= 0
    assert isinstance(row['capture_supported'], bool)
    assert isinstance(row['connection_eligible'], bool)
    assert all(isinstance(code, str) for code in row['finding_codes'])
    assert 'scale-' not in json.dumps(row, sort_keys=True)


def test_capture_measurement_grows_private_linked_lifecycle_records(tmp_path):
    row = asyncio.run(measure_case(100, 'capture', tmp_path=tmp_path))

    assert row['record_counts'] == {'intents': 100, 'attempts': 100, 'outbox': 100}
    assert row['rows_linked'] is True
    assert row['unique_rows'] is True
    assert row['terminal_reservations'] == 0
    assert row['status'] == 'measured'


@pytest.mark.skipif(os.getenv('QWQ_RUN_RECOVERY_SCALE') != '1',
                    reason='set QWQ_RUN_RECOVERY_SCALE=1; invoke one case per timeout-limited process')
@pytest.mark.parametrize('size', SIZES, ids=lambda size: f'size-{size}')
@pytest.mark.parametrize(('kind', 'sweep_phase'), [
    ('capture', None), ('owner', None), ('sweep', 'cold'), ('sweep', 'warm'),
], ids=['capture', 'owner', 'sweep-cold', 'sweep-warm'])
def test_opt_in_measure_case(tmp_path, size, kind, sweep_phase):
    """Long measurement only: a process timeout is censored by its nonzero timeout exit status."""
    row = asyncio.run(measure_case(size, kind, tmp_path=tmp_path, sweep_phase=sweep_phase))
    print(json.dumps(row, sort_keys=True))
    assert row['status'] == 'measured'
