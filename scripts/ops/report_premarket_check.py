#!/usr/bin/env python3
"""장전 증거만 읽는 단발 점검. 호출·발송·주문·운영 변경을 하지 않는다."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import stat
import subprocess
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
MAX_BYTES = 8 * 1024 * 1024
ROLES = ('report_one_line', 'morning_brief')


def safe_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute_path_required')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink_rejected')
    return path


def read_bounded(path, limit=MAX_BYTES):
    path = safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('input_size_or_type')
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
        if len(data) > limit or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('input_changed_or_oversize')
        return data


def audit_rows(path):
    try:
        rows = [json.loads(line) for line in read_bounded(path).splitlines() if line.strip()]
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError('invalid_rows')
        return rows, 'read'
    except FileNotFoundError:
        return [], 'missing'
    except (ValueError, OSError):
        return [], 'invalid'


def stamp(value):
    dt = datetime.fromisoformat(value)
    # 운영 역할 감사/아카이브의 기존 naive 시각은 호스트 KST로 해석한다.
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def summarize_roles(rows, now):
    now = now.astimezone(KST)
    output = {}
    for role in ROLES:
        found = []
        bad_timestamp = 0
        for row in rows:
            try:
                when = stamp(row.get('ts'))
            except (TypeError, ValueError):
                bad_timestamp += row.get('task') == role
                continue
            if row.get('task') == role and when.date() == now.date() and when.hour >= 7 and when <= now:
                found.append(row)
        duplicate_rows = len(found) - len({json.dumps(r, sort_keys=True) for r in found})
        verified = [r for r in found if r.get('provider') == 'gemini' and r.get('request_sent') is True
                    and r.get('refusal_detected') is False
                    and r.get('role_policy_version') == 'engine-roles-v1'
                    and r.get('requested_model') == 'gemini-3.1-flash-lite'
                    and isinstance(r.get('resolved_model'), str) and r['resolved_model'].startswith('gemini-3.1-flash-lite')
                    and r.get('requested_thinking_level') == 'minimal' and r.get('outcome') == 'success'
                    and r.get('usage_known') is True and r.get('finish_reason') == 'STOP'
                    and all(finite(r.get(k)) for k in ('input_tokens', 'output_tokens', 'elapsed_ms', 'estimated_cost_usd'))]
        output[role] = {
            'status': 'review_required' if duplicate_rows or bad_timestamp else 'not_observed' if not found else 'call_metadata_verified' if len(verified) == len(found) else 'review_required',
            'observed_rows': len(found), 'verified_metadata_rows': len(verified),
            'duplicate_rows': duplicate_rows, 'invalid_timestamp_rows': bad_timestamp,
            'resolved_models': sorted({r['resolved_model'] for r in verified}),
            'input_tokens': sum(r['input_tokens'] for r in found) if found and not duplicate_rows and all(finite(r.get('input_tokens')) for r in found) else None,
            'output_tokens': sum(r['output_tokens'] for r in found) if found and not duplicate_rows and all(finite(r.get('output_tokens')) for r in found) else None,
            'max_elapsed_ms': max((r['elapsed_ms'] for r in found), default=None) if all(finite(r.get('elapsed_ms')) for r in found) else None,
            'estimated_cost_usd': str(sum((Decimal(str(r['estimated_cost_usd'])) for r in found), Decimal('0'))) if found and not duplicate_rows and all(finite(r.get('estimated_cost_usd')) for r in found) else None,
        }
    output['post_exit_research'] = {'status': 'not_due_friday' if now.weekday() == 4 else 'not_evaluated'}
    return output


def brief_evidence(path, day, *, now=None):
    cutoff = now if now is not None else datetime.now(timezone.utc)
    result = {'status': 'missing_or_invalid', 'dispatch_status': 'unknown', 'generated_count': None,
              'delivery_proof': 'application_record_only_not_recipient_receipt'}
    try:
        data = json.loads(read_bounded(path))
        if data.get('kr_date') != day:
            return result
        generated, sent = data.get('generated'), data.get('dispatch')
        if not isinstance(generated, list) or not isinstance(sent, list):
            return result
        valid = [r for r in sent if isinstance(r, dict) and isinstance(r.get('brief_ref'), dict)
                 and r.get('status') in ('sent', 'failed') and stamp(r['sent_at']).date().isoformat() == day
                 and stamp(r['sent_at']) <= cutoff
                 and any(isinstance(g, dict) and type(g.get('version')) is int
                         and g.get('text_sha256') and g['version'] == r['brief_ref'].get('version')
                         and g['text_sha256'] == r['brief_ref'].get('text_sha256')
                         and stamp(g['generated_at']).date().isoformat() == day
                         and stamp(g['generated_at']) <= stamp(r['sent_at']) for g in generated)]
        result.update(status='read', generated_count=len(generated),
                      dispatch_status=valid[-1]['status'] if valid else 'not_observed')
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return result


def command(argv):
    # 고정된 git/systemctl 읽기 명령만 호출하며 토큰/환경을 상속하지 않는다.
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10,
                            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TZ': 'Asia/Seoul'})
    if result.returncode or len(result.stdout) > 16384:
        raise ValueError('read_command_failed')
    return result.stdout.strip()


def service(name, fields):
    raw = command(['/usr/bin/systemctl', 'show', name, '--property=' + ','.join(fields)])
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def health_evidence(data):
    if not isinstance(data, dict):
        raise ValueError('health_shape_invalid')
    broker, risk, recovery = (data.get(k) or {} for k in ('broker', 'risk_manager', 'execution_recovery'))
    if not all(isinstance(value, dict) for value in (broker, risk, recovery)):
        raise ValueError('health_shape_invalid')
    count = lambda x: x if type(x) is int and x >= 0 else None
    symbols = recovery.get('unattributed_symbols')
    result = {'connected': broker.get('connected') is True,
              'broker_pending': count(broker.get('pending_orders')),
              'risk_pending': count(risk.get('pending_orders')),
              'journal_pending': count(recovery.get('journal_pending_count')),
              'recovery_ready': recovery.get('status') == 'ready',
              'unattributed_symbol_count': len(symbols) if isinstance(symbols, list) else count(symbols)}
    result['healthy'] = (result['connected'] and result['recovery_ready']
                         and all(result[k] == 0 for k in ('broker_pending', 'risk_pending', 'journal_pending', 'unattributed_symbol_count')))
    return result


def local_health():
    # 기존 봇의 메모리 상태만 읽는다. 증권사 조회/자격증명/리다이렉트 없음.
    connection = http.client.HTTPConnection('127.0.0.1', 8080, timeout=5)
    try:
        connection.request('GET', '/api/health')
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 1024 * 1024:
            raise ValueError('local_health_unavailable')
        return health_evidence(json.loads(raw))
    finally:
        connection.close()


def collect(manifest, now):
    result = {'schema_version': 'premarket-check-v1', 'observed_at': now.isoformat(),
              'stage': manifest['stage'], 'status': 'review_required', 'account_net_profit': None,
              'no_paid_calls': True, 'no_production_mutation': True, 'naive_audit_timezone': 'Asia/Seoul'}
    if manifest['stage'] == 'roles':
        rows, status = audit_rows(Path(manifest['audit_path']))
        result['audit_status'] = status
        result['roles'] = summarize_roles(rows, now)
        result['brief'] = brief_evidence(Path(manifest['brief_archive']), manifest['date'], now=now)
        if status == 'read' and all(result['roles'][r]['status'] == 'call_metadata_verified' for r in ROLES) and result['brief']['dispatch_status'] == 'sent':
            result['status'] = 'evidence_collected'
    elif manifest['stage'] == 'preopen':
        repo = safe_path(manifest['repository'])
        head = command(['/usr/bin/git', '-C', str(repo), 'rev-parse', 'HEAD'])
        result['head_matches'] = head == manifest['expected_head']
        mismatches = []
        for raw, expected in manifest['file_pins'].items():
            try:
                actual = hashlib.sha256(read_bounded(raw)).hexdigest()
            except (OSError, ValueError):
                actual = None
            if actual != expected:
                mismatches.append(raw)
        result['file_pin_count'] = len(manifest['file_pins'])
        result['file_pin_mismatches'] = mismatches
        result['bot'] = service('qwq-ai-trader', ('ActiveState', 'MainPID', 'NRestarts'))
        result['pid_matches'] = result['bot'].get('MainPID') == str(manifest['expected_pid'])
        result['timer'] = service('qwq-entry-capture-20261016.timer', ('ActiveState', 'UnitFileState', 'NextElapseUSecRealtime', 'LastTriggerUSec'))
        result['kill_switch_present'] = safe_path(manifest['kill_switch']).is_file()
        result['new_receipt_absent'] = not safe_path(manifest['capture_receipt']).exists()
        result['local_health'] = local_health()
        usage = os.statvfs(repo)
        result['disk_free_bytes'] = usage.f_bavail * usage.f_frsize
        expected = manifest['expected_timer_time']
        if (result['head_matches'] and result['pid_matches'] and not mismatches and result['file_pin_count'] > 0
                and result['bot']['ActiveState'] == 'active' and result['bot']['NRestarts'] == '0'
                and result['timer']['ActiveState'] == 'active' and result['timer']['UnitFileState'] == 'enabled'
                and result['timer']['NextElapseUSecRealtime'] == expected
                and result['kill_switch_present'] and result['new_receipt_absent']
                and result['local_health']['healthy']
                and result['disk_free_bytes'] >= manifest['min_free_bytes']):
            result['status'] = 'evidence_collected'
        result['broker_recovery_and_pending'] = 'local_bot_memory_not_exchange_order_query'
    else:
        raise ValueError('invalid_stage')
    return result


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write('\n'); handle.flush(); os.fsync(handle.fileno())


def run(manifest, now):
    if now.tzinfo is None:
        raise ValueError('aware_time_required')
    if any(datetime.fromisoformat(manifest[key]).tzinfo is None for key in ('not_before', 'not_after')):
        raise ValueError('aware_window_required')
    start, end = stamp(manifest['not_before']), stamp(manifest['not_after'])
    if start.date().isoformat() != manifest['date'] or end.date() != start.date() or not start <= now <= end:
        return 2
    target = safe_path(manifest['output_dir'])
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.stat().st_uid != os.getuid() or stat.S_IMODE(target.stat().st_mode) != 0o700:
        raise ValueError('private_output_required')
    try:
        write_private(target / 'attempted.json', {'started_at': now.isoformat(), 'stage': manifest['stage']})
    except FileExistsError:
        return 3
    directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    try:
        result = collect(manifest, now)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException, subprocess.SubprocessError):
        result = {'status': 'read_failed', 'stage': manifest['stage'], 'observed_at': now.isoformat()}
    write_private(target / 'result.json', result)
    fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return 0 if result['status'] == 'evidence_collected' else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args()
    try:
        return run(json.loads(read_bounded(args.manifest, 1024 * 1024)), datetime.now(timezone.utc))
    except (OSError, ValueError, KeyError, TypeError):
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
