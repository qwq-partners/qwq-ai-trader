"""명시한 첫 스캔 관측의 레코드 예산. 파일·운영 상태 접근 없음."""
from __future__ import annotations

from .entry_observation_journal import (MAX_LINE_BYTES, BOUNDED_ENVELOPE_VERSION,
                                       CONTROL_ROW_MAX_BYTES, journal_record_envelope_bytes)
from .entry_gate_trace import validate_settings as validate_gate
from .selection_basis import validate_settings as validate_selection

FIRST_SCAN_VERSION = 'runner-first-scan-v4'
FIRST_SCAN_RETAINED_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024


def first_scan_record_byte_limits(settings):
    """명시 종류별 직렬화 상한. 선정 근거는 전체 레코드 상한을 유지한다."""
    if 'journal_budget_version' in settings and (
            settings['journal_budget_version'] != BOUNDED_ENVELOPE_VERSION
            or settings.get('version') != FIRST_SCAN_VERSION
            or 'record_allocations' not in settings or 'record_max_bytes' not in settings):
        raise ValueError('명시 journal budget에는 첫 스캔 종류별 개수/바이트 계약 필요')
    if 'record_max_bytes' not in settings:
        return None
    if settings.get('version') != FIRST_SCAN_VERSION or 'record_allocations' not in settings:
        raise ValueError('종류별 byte 한도는 명시 첫 스캔 예산과 함께 필요')
    limits = settings['record_max_bytes']
    maximum = settings.get('max_record_bytes')
    if (type(maximum) is not int or type(limits) is not dict
            or set(limits) != {'scan_metadata', 'ws_quote', 'market_state', 'other'}
            or any(type(v) is not int or not 0 < v <= maximum for v in limits.values())
            or limits['scan_metadata'] != maximum):
        raise ValueError('종류별 record byte 한도/선정 근거 상한 불일치')
    return dict(limits)


def first_scan_journal_bound(settings, limits):
    """종류별 개수·바이트를 함께 강제할 때만 가중 저장 예산을 사용한다."""
    byte_limits = first_scan_record_byte_limits(settings)
    if 'journal_budget_version' in settings:
        envelope = journal_record_envelope_bytes(settings['buffer_capacity'])
        return sum(limits[k] * (byte_limits[k] + envelope) for k in limits) + 2 * CONTROL_ROW_MAX_BYTES
    if byte_limits is None:
        return settings['buffer_capacity'] * (settings['max_record_bytes'] + 2048) + 2048
    return sum(limits[k] * (byte_limits[k] + 2048) for k in limits) + 2048


def first_scan_record_limits(settings):
    """기존 계약은 유지하고 명시 v4 예산만 검사한다. 실제 유량 검증은 별도다."""
    first_scan_record_byte_limits(settings)  # 지원하지 않는 조합은 조기 반환 전 거부한다.
    if settings.get('version') != FIRST_SCAN_VERSION or 'record_allocations' not in settings:
        return None
    allocations = settings['record_allocations']
    if (type(allocations) is not dict or set(allocations) != {'ws_quote', 'market_state', 'other'}
            or any(type(v) is not int or v <= 0 for v in allocations.values())):
        raise ValueError('첫 스캔 record allocation은 종류별 양의 정수 필요')
    selection = validate_selection(settings.get('selection_basis'))
    gate = validate_gate(settings.get('entry_gate_trace'))
    if (gate['max_candidates'] < selection['max_candidates']
            or gate['source_version_ref'] != settings.get('source_version_ref')
            or gate['configuration_ref'] != settings.get('configuration_ref')):
        raise ValueError('첫 스캔 선정/게이트 범위·출처 불일치')
    channels = settings.get('channels')
    if (type(channels) is not dict or channels.get('market_state_evidence') is not True
            or type(channels.get('market_state_max_records')) is not int
            or channels['market_state_max_records'] != allocations['market_state']):
        raise ValueError('첫 스캔 market-state allocation 결합 불일치')
    limits = {**allocations, 'scan_metadata': 1 + selection['max_candidates'] + gate['max_candidates']}
    if (any(type(settings.get(k)) is not int or settings[k] <= 0 for k in
            ('buffer_capacity', 'queue_capacity', 'max_bytes', 'max_record_bytes'))
            or not 65536 <= settings['max_record_bytes'] <= MAX_LINE_BYTES - 2048
            or settings['buffer_capacity'] < sum(limits.values())
            or settings['queue_capacity'] < settings['buffer_capacity']
            or settings['max_bytes'] < first_scan_journal_bound(settings, limits)):
        raise ValueError('첫 스캔 종류별 레코드·큐·저장 예산 부족')
    return limits
