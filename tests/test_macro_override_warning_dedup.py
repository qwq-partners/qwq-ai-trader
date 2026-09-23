"""만료값은 계속 제외하고 같은 만료 자료의 경고만 중복 억제한다."""
import json
from datetime import datetime as RealDatetime

import pytest
from loguru import logger

from src.experts import macro_economist as module
from src.experts.types import ExpertConfig


class Clock(RealDatetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 23, 13, 0, 0)


@pytest.fixture
def setup_overrides(tmp_path, monkeypatch):
    path = tmp_path / 'overrides.json'
    monkeypatch.setattr(module, '_MANUAL_OVERRIDE_PATH', path)
    monkeypatch.setattr(module, 'datetime', Clock)
    warnings = []
    sink = logger.add(lambda m: warnings.append(str(m)), level='WARNING', format='{message}')
    try:
        yield path, warnings
    finally:
        logger.remove(sink)


def expired(value=3.2, deadline='2026-06-12'):
    return {'value': value, 'valid_until': deadline}


def load():
    # 전문가 인스턴스가 바뀌어도 같은 프로세스의 동일 경고를 다시 쌓지 않는다.
    return module.MacroEconomist(ExpertConfig())._load_manual_overrides()


def write(path, data):
    path.write_text(json.dumps(data), encoding='utf-8')


def test_same_twelve_expired_fields_warn_once_but_never_apply(setup_overrides):
    path, warnings = setup_overrides
    write(path, {f'field_{i}': expired() for i in range(12)})
    for _ in range(12):
        assert load() == {}
    assert len(warnings) == 1
    assert '12' in warnings[0]


@pytest.mark.parametrize('changed', [expired(value=4.1), expired(deadline='2026-06-13')])
def test_changed_expired_value_or_deadline_warns_again(setup_overrides, changed):
    path, warnings = setup_overrides
    write(path, {'cpi_yoy': expired()})
    assert load() == {}
    write(path, {'cpi_yoy': changed})
    assert load() == {}
    assert load() == {}
    assert len(warnings) == 2


@pytest.mark.parametrize('intermediate', [{}, {'cpi_yoy': expired(3.2, '2026-09-24')}])
def test_removed_or_valid_override_can_warn_on_reintroduction(setup_overrides, intermediate):
    path, warnings = setup_overrides
    write(path, {'cpi_yoy': expired()})
    assert load() == {}
    write(path, intermediate)
    assert load() == ({'cpi_yoy': 3.2} if intermediate else {})
    write(path, {'cpi_yoy': expired()})
    assert load() == {}
    assert load() == {}
    assert len(warnings) == 2


def test_file_removal_then_reintroduction_rearms_warning(setup_overrides):
    path, warnings = setup_overrides
    write(path, {'cpi_yoy': expired()})
    assert load() == {}
    path.unlink()
    assert load() == {}
    write(path, {'cpi_yoy': expired()})
    assert load() == {}
    assert load() == {}
    assert len(warnings) == 2


def test_warning_does_not_dump_values_or_newline_keys(setup_overrides):
    path, warnings = setup_overrides
    write(path, {'bad\nkey': expired('private-synthetic-value')})
    assert load() == {}
    assert len(warnings) == 1
    assert 'private-synthetic-value' not in warnings[0]
    assert 'bad\nkey' not in warnings[0]
