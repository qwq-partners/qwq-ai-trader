"""Test-only contracts for the opt-in recovery diagnostics scale harness."""

import asyncio
import json
import os

import pytest

from recovery_scale_harness import (
    FORBIDDEN_SWEEP_SEAMS, SIZES, _forbid_sweep_writers, _measure,
    _validate_unchanged, classify_measurement, measure_case,
)


def test_report_distinguishes_unavailable_from_fast_success():
    row = classify_measurement(snapshot_stable=None, wall_max_ms=1.0)

    assert row['capture_supported'] is False
    assert row['connection_eligible'] is False


def test_fast_stable_measurement_is_connection_eligible():
    row = classify_measurement(snapshot_stable=True, wall_max_ms=50.0)

    assert row == {'capture_supported': True, 'connection_eligible': True}


def test_measurement_validates_after_its_timed_window():
    events = []

    def reset():
        events.append('reset')

    def operation():
        events.append('operation')
        return {'value': 1}

    def validate(value):
        events.append(('validate', value))

    asyncio.run(_measure(operation, reset, validate))

    assert events.count('operation') == 4  # three normal trials plus one tracing trial
    assert all(events[index + 1] == ('validate', {'value': 1})
               for index, item in enumerate(events[:-1]) if item == 'operation')


def test_measurement_marks_each_operation_and_validation_phase(capsys):
    asyncio.run(_measure(lambda: {'value': 1}, lambda: None, lambda _value: None))

    phases = [json.loads(line)['scale_phase'] for line in capsys.readouterr().out.splitlines()]
    assert phases == ['reset', 'normal', 'validation'] * 3 + [
        'reset', 'tracing', 'validation',
    ]


def test_measurement_validates_async_tracing_return_value():
    values = []

    async def operation():
        return {'async': True}

    asyncio.run(_measure(operation, lambda: None, values.append))

    assert values == [{'async': True}] * 4


def test_setup_has_its_marker_before_fixture_work(tmp_path, monkeypatch, capsys):
    import test_execution_runtime as fixture_module

    original = fixture_module.setup

    async def checked_setup(path):
        lines = capsys.readouterr().out.splitlines()
        assert lines and json.loads(lines[-1]) == {'scale_phase': 'setup'}
        return await original(path)

    monkeypatch.setattr(fixture_module, 'setup', checked_setup)
    asyncio.run(measure_case(0, 'capture', tmp_path=tmp_path))
    phases = [json.loads(line)['scale_phase'] for line in capsys.readouterr().out.splitlines()]
    assert 'warm' not in phases
    assert 'baseline' in phases


def test_validation_rejects_state_mutation_outside_measurement_window():
    class Owner:
        _state = {'value': 1}

    owner = Owner()
    baseline = {'value': 1}
    owner._state['value'] = 2

    with pytest.raises(AssertionError):
        _validate_unchanged(owner, baseline)


def test_forbidden_sweep_guard_blocks_store_writer_and_declares_seams():
    class Target:
        async def mutate(self, *_args):
            raise AssertionError('unpatched')

        async def prepare(self, *_args):
            raise AssertionError('unpatched')

        async def claim(self, *_args):
            raise AssertionError('unpatched')

        async def record_result(self, *_args):
            raise AssertionError('unpatched')

        async def release_protection_pending(self, *_args):
            raise AssertionError('unpatched')

        async def resume_protection_admission(self):
            raise AssertionError('unpatched')

    class Store:
        async def commit(self, *_args):
            raise AssertionError('unpatched')

    runtime = Target()
    runtime.owner = Target()
    runtime.owner.store = Store()
    runtime.lifecycle = Target()
    runtime.gateway = None
    producer = Target()
    producer._submit = runtime.release_protection_pending

    async def scenario():
        with _forbid_sweep_writers(runtime, producer) as installed:
            assert installed == FORBIDDEN_SWEEP_SEAMS
            with pytest.raises(AssertionError, match='forbidden writer'):
                await runtime.owner.store.commit()

    asyncio.run(scenario())
    async def restored():
        with pytest.raises(AssertionError, match='unpatched'):
            await runtime.owner.store.commit()
    asyncio.run(restored())
    assert FORBIDDEN_SWEEP_SEAMS == {
        'owner.mutate', 'owner.store.commit', 'lifecycle.prepare', 'lifecycle.claim',
        'lifecycle.record_result', 'runtime.release_protection_pending',
        'runtime.resume_protection_admission', 'producer._submit', 'gateway.any',
    }


def test_forbidden_sweep_guard_fails_closed_when_a_named_seam_is_missing():
    class Target:
        async def mutate(self, *_args): pass
        async def prepare(self, *_args): pass
        async def record_result(self, *_args): pass
        async def release_protection_pending(self, *_args): pass
        async def resume_protection_admission(self, *_args): pass
        async def _submit(self, *_args): pass

    class Store:
        async def commit(self, *_args): pass

    runtime = Target()
    runtime.owner = Target()
    runtime.owner.store = Store()
    runtime.lifecycle = Target()  # ``claim`` deliberately absent.
    runtime.gateway = None
    with pytest.raises(AssertionError, match='missing forbidden sweep seams'):
        with _forbid_sweep_writers(runtime, Target()):
            pass


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
    assert row['synthetic_finality'] == 'synthetic_lifecycle_rejected'
    assert row['report_bytes'] > 0
    assert row['peak_bytes'] >= 0
    assert row['wall_max_ms'] >= 0
    assert row['first_yield_delay_ms'] >= 0
    assert 'loop_stall_ms' not in row
    assert isinstance(row['capture_supported'], bool)
    assert isinstance(row['connection_eligible'], bool)
    assert all(isinstance(code, str) for code in row['finding_codes'])
    assert 'scale-' not in json.dumps(row, sort_keys=True)
    assert json.dumps(row, sort_keys=True, allow_nan=False)


def test_capture_measurement_grows_private_linked_lifecycle_records(tmp_path):
    row = asyncio.run(measure_case(100, 'capture', tmp_path=tmp_path))

    assert row['record_counts'] == {'intents': 100, 'attempts': 100, 'outbox': 100}
    assert row['rows_linked'] is True
    assert row['unique_rows'] is True
    assert row['terminal_reservations'] == 0
    assert row['cohort'] == {'total_events': 100, 'live_events': 2,
                             'terminal_events': 98, 'symbol_count': 5}
    assert row['synthetic_finality'] == 'synthetic_lifecycle_rejected'
    assert row['status'] == 'measured'
    assert json.dumps(row, allow_nan=False)


@pytest.mark.skipif(os.getenv('QWQ_RUN_RECOVERY_SCALE') != '1',
                    reason='set QWQ_RUN_RECOVERY_SCALE=1; invoke one case per timeout-limited process')
@pytest.mark.parametrize('size', SIZES, ids=lambda size: f'size-{size}')
@pytest.mark.parametrize(('kind', 'sweep_phase'), [
    ('capture', None), ('owner', None), ('sweep', 'cold'), ('sweep', 'warm'),
], ids=['capture', 'owner', 'sweep-cold', 'sweep-warm'])
def test_opt_in_measure_case(tmp_path, size, kind, sweep_phase):
    """Long measurement only: a process timeout is censored by its nonzero timeout exit status."""
    row = asyncio.run(measure_case(size, kind, tmp_path=tmp_path, sweep_phase=sweep_phase))
    print(json.dumps(row, sort_keys=True, allow_nan=False))
    assert row['status'] == 'measured'
