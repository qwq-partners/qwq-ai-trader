"""E04 Bear API 400/800 진단 준비·명시 승인 실행. 운영 배선·기존 정책은 변경하지 않는다.

prepare 는 과거 공개 입력을 복사할 뿐 설정/자격/원장/전송을 열지 않는다. run 은 새 승인 지문·창·
소스·제어를 확인하고 일회성 소비를 영속한 뒤 기존 guard/adapter 만 사용한다. 공유 원장의 이전
비교 정책 충돌은 별도 소유권 작업 전까지 실행 차단 사유다. USD 0.50은 추정 예약 계약이다.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
from types import SimpleNamespace

from . import model_comparison_acceptance as acceptance
from .model_comparison_claude import ClaudeSingleAttemptAdapter, DEADLINE_AT
from .model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy, ComparisonRequest
from .model_comparison_input import operating_role_controls
from ..core.adversarial_validator import BEAR_SUFFIX, BEAR_SYSTEM, _parse_verdict
from ..utils.claude_transport import AnthropicAPIClient

KST = acceptance.KST
CONTRACT = dict(model='claude-opus-5-5', caps=[400, 800], reasoning_effort='low', temperature=None,
                seed=None, timeout_seconds=5.75, billing_channel='api', retries=0, daily_attempt_cap=2,
                pairs_per_day=1, experiment_usd_ceiling='0.50', continue_after_known_max_tokens=True)
_TRANSPORT_ERRORS = ('refusal', 'identity_mismatch', 'policy_violation', 'api_timeout', 'api_http_error',
                     'api_incomplete', 'api_unavailable', 'api_auth_unavailable', 'invalid_request',
                     'client_closed', 'cancelled')
_PROVIDER_ERRORS = ('authentication_error', 'permission_error', 'rate_limit_error', 'invalid_request_error',
                    'overloaded_error', 'api_error')


def _canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _now():
    return datetime.now(KST)


def _monotonic():
    return asyncio.get_running_loop().time()


def _source_commit():
    root = Path(__file__).resolve().parents[2]
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    if git('status', '--porcelain', '--untracked-files=all'):
        raise ValueError('source_not_clean')
    return git('rev-parse', 'HEAD')


def _stamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('aware_time_required')
    return stamp.astimezone(KST)


def _window(plan, now, *, prepare=False):
    start, end = _stamp(plan['start']), _stamp(plan['end'])
    now = _stamp(now.isoformat())
    if start.date() != end.date() or start >= end or now > end or (prepare and now > start):
        raise ValueError('approval_window_invalid')
    if not prepare and not start <= now < end:
        raise ValueError('outside_approval_window')
    return start, end


def _safe_dir(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('unsafe_output_path')
    if path.exists():
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe_output_directory')
    return path


def _write_exclusive(path, value):
    # O_EXCL + fsync 파일/디렉터리: 실패·프로세스 종료 뒤에도 소비 표식을 지우지 않는다.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(_canon(value))
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('unsafe_prepared_file')
    return json.loads(path.read_text(encoding='utf-8'))


def _consume_experiment(ledger, plan, approved_plan_sha256, clock):
    # 같은 원장의 같은 실험 ID는 출력 디렉터리/프로세스와 무관하게 한 번만 소비한다.
    # DB 정책/스키마를 변경하지 않는다. 실패·종료 뒤에도 이 파일을 삭제하거나 재사용하지 않는다.
    canonical = Path(ledger.path).resolve(strict=True)
    claim = canonical.with_name(canonical.name + '.bear-api-cap-' + _sha(plan['experiment_id']) + '.consumed.json')
    try:
        _write_exclusive(claim, dict(experiment_id=plan['experiment_id'], plan_sha256=approved_plan_sha256,
                                    consumed_at=clock().isoformat()))
    except FileExistsError:
        raise ValueError('experiment_already_consumed') from None


def prepare_diagnostic(*, source_dir, out_dir, experiment_id, source_commit, start, end, now):
    if not isinstance(experiment_id, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', experiment_id) is None:
        raise ValueError('experiment_id_invalid')
    if not isinstance(source_commit, str) or re.fullmatch(r'[0-9a-f]{40}', source_commit) is None or source_commit != _source_commit():
        raise ValueError('source_commit_mismatch')
    _window(dict(start=start, end=end), now, prepare=True)
    old = acceptance.load_prepared(source_dir)
    if experiment_id == old['manifest']['experiment_id']:
        raise ValueError('fresh_experiment_required')
    out = _safe_dir(out_dir)
    if out.exists():
        raise ValueError('fresh_output_directory_required')
    frozen = dict(input_spec=old['input']['input_spec'], operating_controls=old['input']['operating_controls'])
    context = acceptance.common_context_for(frozen['input_spec'])
    if len(context.encode('utf-8')) > 65536:
        raise ValueError('context_too_large')
    plan = dict(schema='bear-api-cap-v1', experiment_id=experiment_id, source_commit=source_commit,
                output_directory=str(out), prepared_at=now.isoformat(), start=start, end=end,
                contract=CONTRACT, input_sha256=_sha(_canon(frozen)),
                operating_controls_sha256=_sha(_canon(frozen['operating_controls'])),
                prompt_sha256=_sha(context + BEAR_SUFFIX), system_sha256=_sha(BEAR_SYSTEM),
                provenance=dict(experiment_id=old['manifest']['experiment_id'], prepared_at=old['input']['prepared_at'],
                                manifest_sha256=old['approval']['manifest_sha256'],
                                input_sha256=_sha(_canon(old['input']))))
    out.mkdir(parents=True, mode=0o700)
    _write_exclusive(out / 'input.json', frozen)
    _write_exclusive(out / 'plan.json', plan)
    return dict(out_dir=str(out), experiment_id=experiment_id, plan_sha256=_sha(_canon(plan)),
                status='prepared_not_authorized', ledger_readiness='not_checked')


def _policy(plan):
    day = _stamp(plan['start']).date().isoformat()
    return ComparisonExecutionPolicy(plan['experiment_id'], 2, ('claude-opus-5-5',), 800, '0.50', day, day, 1, 65536)


def check_ledger_policy(ledger_path, plan):
    """원장을 생성/초기화하지 않는 호환성 확인. 상태/거래/요청 행을 읽지 않는다."""
    path = Path(ledger_path).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        return {'ready': False, 'reason': 'existing_ledger_required'}
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='comparison_policy'").fetchone()
            if not exists:
                return {'ready': True, 'reason': 'no_existing_comparison_policy'}
            rows = db.execute('SELECT policy FROM comparison_policy').fetchall()
            if len(rows) == 1 and rows[0][0] == _policy(plan).serialized():
                return {'ready': True, 'reason': 'matching_policy'}
            return {'ready': False, 'reason': 'existing_policy_conflict'}
    except (sqlite3.Error, OSError, ValueError):
        return {'ready': False, 'reason': 'ledger_preflight_unavailable'}


def _controls(config):
    # LLMManager 생성은 월 원장/다른 전송도 만든다. 기존 순수 모델 해석 메서드만 사용한다.
    from ..utils.llm import LLMManager
    from ..core.cross_validator import SINGLE_FALLBACK_TIMEOUT
    view = SimpleNamespace(config=config)
    view._get_model = lambda provider, weight: LLMManager._get_model(view, provider, weight)
    return operating_role_controls(view, fallback_prompt_suffix='', fallback_timeout=SINGLE_FALLBACK_TIMEOUT)


def _ledger(config):
    from ..utils.llm_budget import MonthlyBudgetLedger
    if not Path(config.budget_state_path).expanduser().is_file():
        raise ValueError('existing_ledger_required')
    return MonthlyBudgetLedger(config.budget_state_path, config.monthly_budget_usd,
                               policy_enabled=config.budget_policy_enabled, missing_cost_policy=config.missing_cost_policy,
                               economy_summary_models=(config.openai_model_light,))


def _load_plan(out, approved_by, approved_plan_sha256, clock):
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError('approval_missing')
    if (out / 'consumed.json').exists():
        raise ValueError('experiment_consumed')
    if {p.name for p in out.iterdir()} != {'input.json', 'plan.json'}:
        raise ValueError('unexpected_output_files')
    plan, frozen = _read(out / 'plan.json'), _read(out / 'input.json')
    if approved_plan_sha256 != _sha(_canon(plan)):
        raise ValueError('approved_plan_mismatch')
    if plan['schema'] != 'bear-api-cap-v1' or _canon(plan['contract']) != _canon(CONTRACT):
        raise ValueError('fixed_contract_mismatch')
    if plan['output_directory'] != str(out):
        raise ValueError('output_directory_mismatch')
    if plan['source_commit'] != _source_commit():
        raise ValueError('source_commit_mismatch')
    _window(plan, clock())
    context = acceptance.common_context_for(frozen['input_spec'])
    if (plan['input_sha256'] != _sha(_canon(frozen))
            or plan['operating_controls_sha256'] != _sha(_canon(frozen['operating_controls']))
            or plan['prompt_sha256'] != _sha(context + BEAR_SUFFIX) or plan['system_sha256'] != _sha(BEAR_SYSTEM)):
        raise ValueError('frozen_input_mismatch')
    return plan, context


def _attempt_summary(cap, result, record, late):
    value = result.value
    known = (record['state'] in ('known', 'known_reconciled') and record['reservation_token'] is not None
             and result.actual_cost_usd is not None and result.settlement == 'measured')
    identity = (value.get('model_identity_status') == 'verified' and value.get('requested_model_match') is True
                and value.get('resolved_model') == CONTRACT['model'])
    base = (not late and known and identity and value.get('usage_known') is True
            and value.get('refusal_detected') is False and value.get('http_status') == 200
            and value.get('request_sent') is True and value.get('billing_status') == 'reported_usage')
    verdict = None
    expected = (base and cap == 400 and value.get('success') is False
                and value.get('transport_error_kind') == 'api_incomplete' and value.get('finish_reason') == 'max_tokens')
    if base and value.get('success') is True and value.get('transport_error_kind') is None and value.get('finish_reason') == 'end_turn':
        parsed = _parse_verdict(value.get('content', ''), 'ACCEPT', 'REJECT')
        verdict = None if parsed is None else ('accept' if parsed else 'reject')
    outcome = 'late' if late else 'valid' if verdict else 'expected_max_tokens' if expected else 'stopped'
    safety = _safe_response_status(value)
    reason = None
    if outcome == 'late':
        reason = 'attempt_deadline_exceeded'
    elif outcome == 'stopped':
        reason = (safety['transport_error_kind'] or ('refusal' if safety['refusal_detected'] else None)
                  or ('model_identity_' + safety['model_identity_status'] if not identity else None)
                  or ('usage_unknown' if value.get('usage_known') is not True else None)
                  or ('settlement_unknown' if not known else None)
                  or ('unexpected_http_status' if value.get('http_status') != 200 else None)
                  or 'invalid_or_incomplete_verdict')
    # 원시 오류문/응답/프롬프트는 영수증에 전달하지 않는다. 외부 문자열도 고정 enum만 내보낸다.
    finish = value.get('finish_reason')
    return dict(max_output_tokens=cap, billing_channel='api', outcome=outcome, verdict=verdict,
                identity_verified=identity, usage_known=value.get('usage_known') is True, stop_reason=reason,
                settlement_known=known, api_cost_usd=str(result.actual_cost_usd) if known else None,
                finish_reason=finish if finish in ('end_turn', 'max_tokens', 'refusal') else None)


def _safe_response_status(value):
    kind, provider, status = (value.get('transport_error_kind'), value.get('provider_error_type'),
                              value.get('http_status'))
    identity = value.get('model_identity_status')
    if identity == 'verified':
        identity = ('verified' if value.get('resolved_model') == CONTRACT['model']
                    and value.get('requested_model_match') is True else 'mismatch')
    elif identity != 'mismatch':
        identity = 'unverified'
    return dict(transport_error_kind=kind if kind in _TRANSPORT_ERRORS else
                ('unclassified_transport_error' if kind is not None else None),
                refusal_detected=value.get('refusal_detected') is True,
                http_status=status if type(status) is int and 100 <= status <= 599 else None,
                model_identity_status=identity,
                provider_error_type=provider if provider in _PROVIDER_ERRORS else None)


def _evidence(attempt_id, cap, record, elapsed, result=None, error=None):
    record = record or {}
    value = result.value if result is not None else {}
    usage = value.get('usage_known') is True
    state = record.get('state')
    safety = _safe_response_status(value)
    return dict(attempt_id=attempt_id, max_output_tokens=cap, billing_channel='api',
                requested_model=CONTRACT['model'],
                resolved_model=CONTRACT['model'] if safety['model_identity_status'] == 'verified' else None,
                input_tokens=value.get('input_tokens') if usage else None,
                output_tokens=value.get('output_tokens') if usage else None,
                elapsed_ms=max(0, elapsed * 1000), reservation_token=record.get('reservation_token'),
                dispatch_entered=record.get('dispatch_entered'),
                billing_status=(value.get('billing_status') if value.get('billing_status') in
                                ('reported_usage', 'not_sent', 'rejected_before_generation') else 'unknown'),
                ledger_state=state if state in ('known', 'known_reconciled', 'unknown', 'blocked',
                                                'pending', 'pending_reconciliation') else None,
                request_sent=value.get('request_sent') if type(value.get('request_sent')) is bool else None,
                settlement_known=False, outcome='error', verdict=None, error=error, **safety)


def _remaining_window(plan, clock):
    now = clock()
    _, end = _window(plan, now)
    if (end - now.astimezone(KST)).total_seconds() < 5.75:
        raise ValueError('insufficient_fixed_attempt_window')
    return end


async def run_diagnostic(*, out_dir, approved_by, approved_plan_sha256, clock=_now,
                         config_loader=acceptance.load_operating_llm_config, controls_reader=_controls,
                         ledger_factory=_ledger, client_factory=AnthropicAPIClient):
    """주입은 오프라인 검사 경계. CLI 실경로는 전용 API 객체 하나만 소유한다."""
    out = _safe_dir(out_dir)
    plan, context = _load_plan(out, approved_by, approved_plan_sha256, clock)
    config = config_loader()
    if _sha(_canon(controls_reader(config))) != plan['operating_controls_sha256']:
        raise ValueError('operating_controls_mismatch')
    _window(plan, clock())
    # 실제 경로는 원장을 열기 전에 읽기 전용 정책 충돌을 확인한다.
    if ledger_factory is _ledger:
        ready = check_ledger_policy(config.budget_state_path, plan)
        if not ready['ready']:
            raise ValueError(ready['reason'])
    try:
        _write_exclusive(out / 'consumed.json', dict(plan_sha256=approved_plan_sha256,
                         approved_by=approved_by, consumed_at=clock().isoformat()))
    except FileExistsError:
        raise ValueError('experiment_consumed') from None
    receipt = dict(experiment_id=plan['experiment_id'], plan_sha256=approved_plan_sha256,
                   source_commit=plan['source_commit'], status='stopped', attempts=[], client_closed=None, error=None)
    client = None
    try:
        ledger = ledger_factory(config)
        guard = ComparisonCallGuard(ledger=ledger, policy=_policy(plan))
        # 새 디렉터리를 만들어도 같은 실험 ID의 완료 응답을 새 진단으로 복구하지 않는다.
        if any(guard.lookup(plan['experiment_id'] + ':api:' + str(cap)) is not None for cap in (400, 800)):
            raise ValueError('experiment_attempt_exists')
        _remaining_window(plan, clock)
        _consume_experiment(ledger, plan, approved_plan_sha256, clock)
        _remaining_window(plan, clock)  # 소비 표식 fsync 지연이 승인 창을 넘겼으면 객체도 생성하지 않는다.
        client = client_factory(config)
        if not isinstance(client, AnthropicAPIClient) or client.billing_channel != 'api':
            raise ValueError('owned_raw_api_required')
        adapter = ClaudeSingleAttemptAdapter(client, clock=clock)
        for cap in (400, 800):
            end = _remaining_window(plan, clock)
            started = _monotonic()
            deadline = started + 5.75
            attempt_id = plan['experiment_id'] + ':api:' + str(cap)
            token = DEADLINE_AT.set(deadline)  # 가드의 동기 예약 시간도 이 절대 시한에 포함한다.
            try:
                request = ComparisonRequest(model='claude-opus-5-5', prompt=context + BEAR_SUFFIX, system=BEAR_SYSTEM,
                                            max_output_tokens=cap, reasoning_effort='low', temperature=None, seed=None,
                                            timeout_seconds=5.75, billing_channel='api')
                async with asyncio.timeout_at(deadline):
                    result = await guard.call(request, dispatch=adapter, attempt_id=attempt_id)
                record = guard.lookup(attempt_id)
                late = _monotonic() >= deadline or clock().astimezone(KST) >= end
                summary = _evidence(attempt_id, cap, record, _monotonic() - started, result)
                summary.update(_attempt_summary(cap, result, record, late))
                receipt['attempts'].append(summary)
                if summary['outcome'] not in ('valid', 'expected_max_tokens'):
                    receipt['error'] = summary['stop_reason']
                    break
            except BaseException as error:
                try:
                    record = guard.lookup(attempt_id)
                except Exception:
                    record = None
                receipt['attempts'].append(_evidence(attempt_id, cap, record, _monotonic() - started,
                                                     error=type(error).__name__))
                raise
            finally:
                DEADLINE_AT.reset(token)
        else:
            receipt['status'] = 'completed'
    except Exception as error:
        # 가드 정책 충돌만 고정 사유로 노출하며 예외 본문은 기록하지 않는다.
        receipt['error'] = ('existing_policy_conflict' if getattr(error, 'reason', None) == 'comparison_policy_mismatch'
                            else 'experiment_already_consumed' if isinstance(error, ValueError)
                            and str(error) == 'experiment_already_consumed'
                            else type(error).__name__)
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                receipt['error'] = 'client_close_failed'
                receipt['status'] = 'stopped'
            session = getattr(client, '_session', None)
            receipt['client_closed'] = (getattr(client, '_closed', False) is True
                                         and (session is None or getattr(session, 'closed', False) is True))
            if not receipt['client_closed']:
                receipt['status'] = 'stopped'
        _write_exclusive(out / 'receipt.json', receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description='E04 Bear API cap diagnostic: prepare is not approval')
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    for name in ('source-dir', 'out', 'experiment-id', 'source-commit', 'start', 'end'):
        prep.add_argument('--' + name, required=True)
    run = sub.add_parser('run')
    for name in ('dir', 'approved-by', 'approved-plan-sha256'):
        run.add_argument('--' + name, required=True)
    check = sub.add_parser('check-ledger')
    check.add_argument('--dir', required=True)
    check.add_argument('--ledger-path', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            result = prepare_diagnostic(source_dir=args.source_dir, out_dir=args.out, experiment_id=args.experiment_id,
                                        source_commit=args.source_commit, start=args.start, end=args.end, now=_now())
        elif args.command == 'check-ledger':
            result = check_ledger_policy(args.ledger_path, _read(_safe_dir(args.dir) / 'plan.json'))
        else:
            result = asyncio.run(run_diagnostic(out_dir=args.dir, approved_by=args.approved_by,
                                                approved_plan_sha256=args.approved_plan_sha256))
    except (ValueError, OSError):
        print(_canon({'status': 'blocked', 'reason': 'preflight_failed'}))
        return 2
    print(_canon(result))
    return 0 if result.get('status') in ('prepared_not_authorized', 'completed') or result.get('ready') is True else 1
