"""KIS 오류 프레임의 고정 분류. 원문/종목/서버 문자열은 결과에 보존하지 않는다."""
from collections import Counter
from copy import deepcopy
import re

from .entry_price_shadow import _timestamp

VERSION = 'kis-frame-diagnostics-v1'
FIELDS = {'version', 'tr_class', 'count_class', 'count', 'encryption', 'scope'}
TR_IDS = {'H0STASP0', 'H0STCNT0', 'H0NXASP0', 'H0NXCNT0'}
REASONS = {'malformed_frame', 'invalid_frame_count', 'unsupported_frame_shape',
           'short_orderbook_frame', 'orderbook_parse_failed'}
SCOPES = {'candidate_lease', 'registered_non_candidate', 'unregistered',
          'candidate_tr_unattributed', 'non_candidate_tr', 'unknown_tr'}


def validate_settings(value):
    if type(value) is not dict or set(value) != {'version'} or value['version'] != VERSION:
        raise ValueError('명시 frame diagnostics 계약 필요')
    return dict(value)


def validate_study_binding(observations, study):
    """설치 시각/신규 경로를 요구하지 않고 저장된 v4 선언의 동일성을 대조한다."""
    if type(study) is not dict or observations.get('evaluation_epoch') != study.get('evaluation_epoch'):
        raise ValueError('연구 세대 불일치')
    capture = study.get('capture', {})
    if type(capture) is not dict:
        raise ValueError('capture 선언 불일치')
    journal = observations.get('journal', {})
    if type(journal) is not dict:
        raise ValueError('원장 metadata 불일치')
    if capture.get('version') == 'runner-first-scan-v4':
        from .selection_basis import validate_settings as selection_settings
        from .entry_gate_trace import validate_settings as gate_settings
        settings = validate_settings(capture.get('frame_diagnostics'))
        selection = selection_settings(capture.get('selection_basis'))
        gate = gate_settings(capture.get('entry_gate_trace'))
        if (gate['source_version_ref'] != capture.get('source_version_ref')
                or gate['configuration_ref'] != capture.get('configuration_ref')
                or gate['max_candidates'] < selection['max_candidates']):
            raise ValueError('v4 기존 선정/진입 선언 불일치')
        if (observations.get('frame_diagnostics') != settings
                or journal.get('format') != 'entry-observation-journal-v2'
                or journal.get('frame_diagnostics') != settings):
            raise ValueError('v4 연구/원장 진단 계약 불일치')
    elif ('frame_diagnostics' in capture or 'frame_diagnostics' in observations
          or 'frame_diagnostics' in journal or journal.get('format') == 'entry-observation-journal-v2'):
        raise ValueError('진단은 명시 v4 연구 전용')


def classify_frame(*, owner, parts=None, tr_id=None, count=None, encrypted=None, data=None):
    """현재 lease 수요만 읽는다. ACK/실수신이나 첫 종목의 다중 프레임 귀속이 아니다."""
    if parts is not None:
        encrypted = parts[0] if len(parts) > 0 else None
        tr_id = parts[1] if len(parts) > 1 else None
        count = parts[2] if len(parts) > 2 else None
        data = parts[3] if len(parts) > 3 else None
    tr_class = tr_id if tr_id in TR_IDS else 'other' if tr_id else 'unknown'
    encryption = 'plain' if encrypted == '0' else 'encrypted' if encrypted == '1' else 'unknown'
    try:
        if type(count) is not int:
            if type(count) is not str or len(count) > 4300:
                raise ValueError
            count = int(count)
        if not 1 <= count <= 999:
            count_class, count = 'out_of_range', None
        else:
            count_class = 'single' if count == 1 else 'multiple'
    except (ValueError, TypeError):
        count_class, count = 'invalid', None
    if tr_class in ('unknown', 'other'):
        scope = 'unknown_tr'
    elif tr_class != 'H0STASP0':
        scope = 'non_candidate_tr'
    else:
        scope = 'candidate_tr_unattributed'
        # 앞 7글자만 조사하여 큰 payload 복사/비정상 종목의 zfill 추정을 피한다.
        symbol = data[:7].split('^', 1)[0] if type(data) is str else None
        if encryption == 'plain' and count_class == 'single' and symbol is not None and re.fullmatch(r'[0-9]{6}', symbol):
            now = owner.clock()
            if not owner._observation_ended and any(v.symbol == symbol and v.deadline > now for v in owner._leases.values()):
                scope = 'candidate_lease'
            elif (tr_id, symbol) in owner._states:
                scope = 'registered_non_candidate'
            else:
                scope = 'unregistered'
    return dict(version=VERSION, tr_class=tr_class, count_class=count_class, count=count,
                encryption=encryption, scope=scope)


def validate_diagnostic(value, *, reason):
    if type(value) is not dict or set(value) != FIELDS or value['version'] != VERSION or reason not in REASONS:
        raise ValueError('frame 진단 필드/사유 불일치')
    tr, cc, count, encryption, scope = (value[k] for k in ('tr_class','count_class','count','encryption','scope'))
    if (type(tr) is not str or tr not in TR_IDS | {'other','unknown'}
            or type(cc) is not str or cc not in {'single','multiple','out_of_range','invalid'}
            or type(encryption) is not str or encryption not in {'plain','encrypted','unknown'}
            or type(scope) is not str or scope not in SCOPES):
        raise ValueError('frame 진단 enum 불일치')
    if ((cc == 'single' and (type(count) is not int or count != 1))
            or (cc == 'multiple' and (type(count) is not int or not 2 <= count <= 999))
            or (cc in ('out_of_range','invalid') and count is not None)):
        raise ValueError('frame count 분류 불일치')
    expected = 'unknown_tr' if tr in ('other','unknown') else 'non_candidate_tr' if tr != 'H0STASP0' else None
    if expected is not None:
        if scope != expected: raise ValueError('frame TR/scope 불일치')
    elif (scope not in {'candidate_lease','registered_non_candidate','unregistered','candidate_tr_unattributed'}
            or ((encryption != 'plain' or cc != 'single') and scope != 'candidate_tr_unattributed')):
        raise ValueError('frame 귀속 근거 부족')
    if reason == 'invalid_frame_count' and cc != 'invalid':
        raise ValueError('파싱 실패 count 분류 불일치')
    if reason == 'unsupported_frame_shape' and (cc == 'invalid' or (encryption == 'plain' and cc == 'single')):
        raise ValueError('지원하지 않는 shape 사유 불일치')
    if reason == 'malformed_frame' and tr == 'H0STASP0' and scope != 'candidate_tr_unattributed':
        raise ValueError('불완전 헤더의 종목 귀속 금지')
    if reason in {'short_orderbook_frame','orderbook_parse_failed'} and tr in {'H0STCNT0','H0NXCNT0','other'}:
        raise ValueError('호가 오류의 TR 불일치')
    return dict(value)


def build_frame_report(observations, *, as_of):
    """원래 순서/시각/선택 계약과 대사. 진단 상세로 connection gap을 정상화하지 않는다."""
    report_at = _timestamp(as_of, '보고 시각')
    if (type(observations) is not dict or type(observations.get('schema_version')) is not int
            or observations['schema_version'] != 1):
        raise ValueError('관측 schema 불일치')
    epoch = observations.get('evaluation_epoch')
    if type(epoch) is not str or not epoch.strip() or len(epoch) > 200:
        raise ValueError('평가 세대 필요')
    settings = observations.get('frame_diagnostics')
    if 'frame_diagnostics' in observations: validate_settings(settings)
    journal = observations.get('journal', {})
    if type(journal) is not dict: raise ValueError('원장 metadata 불일치')
    if journal.get('format') == 'entry-observation-journal-v2':
        if settings is None or journal.get('frame_diagnostics') != settings:
            raise ValueError('원장 진단 계약 불일치')
    evidence_end = report_at
    if journal.get('capture_closed_at') is not None:
        evidence_end = _timestamp(journal['capture_closed_at'], '봉인 시각')
        if evidence_end > report_at: raise ValueError('미래 봉인')
    start = _timestamp(journal['created_at'], '원장 시작') if 'created_at' in journal else None
    if start is not None and start > evidence_end: raise ValueError('원장 경계 역전')
    complete = observations.get('complete'); dropped = observations.get('dropped_records')
    reasons = observations.get('incomplete_reasons')
    if (type(complete) is not bool or type(reasons) is not list or len(reasons) > 100
            or any(type(r) is not str or len(r) > 200 for r in reasons)
            or (dropped is not None and (type(dropped) is not int or dropped < 0))):
        raise ValueError('관측 완전성 계약 불일치')
    persistence = journal.get('persistence_dropped_records')
    if persistence is not None and (type(persistence) is not int or persistence < 0):
        raise ValueError('영속 손실 개수 불일치')
    if complete and (reasons or dropped != 0 or persistence not in (None,0) or journal.get('sealed') is False):
        raise ValueError('완전성/손실 모순')
    records = observations.get('records')
    if type(records) is not list or len(records) > 1000000: raise ValueError('유한 records 필요')
    counts = dict(connection_gaps=0, eligible_frame_gaps=0, available=0, unavailable=0)
    axes = {k: Counter() for k in ('reason','tr_class','count_class','encryption','scope')}
    details = []; previous = start
    for seq, row in enumerate(records, 1):
        if type(row) is not dict or type(row.get('sequence')) is not int or row['sequence'] != seq:
            raise ValueError('원래 sequence 불일치')
        if 'observed_at' in row:
            at = _timestamp(row['observed_at'], '관측 시각')
            if at > evidence_end or (previous is not None and at < previous):
                raise ValueError('관측 인과/봉인/보고 시각 불일치')
            previous = at
        gap = row.get('kind') == 'quote_subscription' and row.get('status') == 'connection_gap'
        detail = row.get('frame_diagnostic')
        if 'frame_diagnostic' in row:
            if not gap or settings is None or 'observed_at' not in row:
                raise ValueError('선언되지 않은 frame 진단')
            detail = validate_diagnostic(detail, reason=row.get('reason'))
        if not gap: continue
        counts['connection_gaps'] += 1
        if complete: raise ValueError('connection gap은 완전 수집이 아니다')
        reason = row.get('reason')
        if reason is not None and (type(reason) is not str or re.fullmatch(r'[a-z][a-z0-9_]{0,79}', reason) is None):
            raise ValueError('고정 gap 사유 필요')
        details.append(dict(sequence=seq, reason=reason,
            status='available' if detail is not None else 'unavailable', frame_diagnostic=deepcopy(detail)))
        if reason not in REASONS: continue
        counts['eligible_frame_gaps'] += 1
        counts['available' if detail is not None else 'unavailable'] += 1
        axes['reason'][row['reason']] += 1
        for key in ('tr_class','count_class','encryption','scope'):
            axes[key][detail[key] if detail is not None else 'unavailable'] += 1
    return dict(schema_version='kis-frame-diagnostics-report-v1', evaluation_epoch=epoch,
        as_of=report_at.isoformat(), diagnostics_expected=settings is not None, counts=counts,
        aggregates={k:dict(v) for k,v in axes.items()}, gaps=details,
        stream_complete=False if counts['connection_gaps'] or not complete else None,
        source_complete=complete, dropped_records=dropped, persistence_dropped_records=persistence,
        incomplete_reasons=list(reasons), journal=deepcopy(journal),
        source_authenticity_verified=False, production_eligible=False, profit_comparison_available=False,
        scope_basis='current_candidate_lease_demand_not_ack_or_received_data')
