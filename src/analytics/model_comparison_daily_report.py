"""모델 비교 실험의 일별 읽기 전용 보고와 최악 예약 상한 계산 (발송·호출·주문 없음).

선정은 봉투(공급자 호출 전 정보)만으로 정한다 — 응답·가격·손익을 바꿔도 표본이 같다.
비용은 고유 attempt_id 를 한 번씩 센다. arm 의 api_cost_usd 는 공통 Bear 를 포함한 시나리오
비용이므로 합산하지 않는다. 조건부 경제성은 compare_policy_outcomes 결과를 그대로 싣고,
없으면 null 과 사유다. 체결·실현손익·계좌 수익이 아니다.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from .entry_price_shadow import _timestamp

SCHEMA_VERSION = 'model-comparison-daily-v1'
KST = ZoneInfo('Asia/Seoul')
PAIR_SCHEMAS = ('model-comparison-pair-v1', 'model-comparison-pair-v2')
# 이 사유가 그날 나타나면 실험 중단 조건 도달로 표시한다 (I1 고정 사유 + 기존 월 예산 사유)
STOP_REASONS = frozenset({
    'comparison_daily_attempt_cap', 'comparison_policy_disabled', 'comparison_policy_mode_not_normal',
    'comparison_economy_threshold', 'comparison_experiment_ceiling', 'comparison_experiment_expired',
    'comparison_policy_mismatch', 'essential_budget_reserved', 'monthly_limit_exceeded',
})


def _kst_day(value) -> str:
    return _timestamp(value, 'time').astimezone(KST).date().isoformat()


def worst_case_reservation(manifest: Mapping, *, on_date: date | None = None) -> dict:
    """실험 manifest 토폴로지의 쌍당 최악 시도 수와 예약 추정액(llm_budget.reserve 와 같은 식).

    입력 추정 = 2×(prompt UTF-8 바이트 + system 바이트 + 256) 토큰, 출력 예약 = 상한(Gemini 2배).
    prompt = 공통 입력 상한(max_common_context_bytes) + 역할 접미. 청구 보증이 아니다.
    """
    from ..core.adversarial_validator import BEAR_SUFFIX, BEAR_SYSTEM, BULL_SUFFIX, BULL_SYSTEM
    from ..utils.llm_budget import _SCALE, _units
    from ..utils.llm_pricing import rate_quote

    roles = manifest['roles']
    context = manifest['max_common_context_bytes']
    retries = manifest['empty_retries']
    when = on_date or date.fromisoformat(manifest['valid_from'])
    attempts = []

    def add(role, control, suffix, system):
        prompt_bytes = context + len(suffix.encode('utf-8'))
        input_tokens = 2 * (prompt_bytes + len(system.encode('utf-8')) + 256)
        output = control['max_output_tokens'] * (2 if control['model'].startswith('gemini-') else 1)
        quote = rate_quote(control['model'], input_tokens, output, on_date=when)
        if quote is None:
            raise ValueError(f"unpriced_model:{control['model']}")
        usd = Decimal(_units(quote.cost_usd)) / _SCALE
        attempts.append({'role': role, 'model': control['model'], 'input_tokens': input_tokens,
                         'output_reserve_tokens': output, 'reservation_usd': str(usd)})

    for index in range(1 + retries):
        add(f'bear:{index}', roles['bear'], BEAR_SUFFIX, BEAR_SYSTEM)
    for arm in ('current', 'candidate'):
        for index in range(1 + retries):
            add(f'bull_{arm}:{index}', roles[f'bull_{arm}'], BULL_SUFFIX, BULL_SYSTEM)
        for index, control in enumerate(roles['fallback_chain']):
            add(f'{arm}_fallback:{index}', control, manifest['fallback_prompt_suffix_worst_case'],
                manifest['fallback_system'])
    per_pair = sum((Decimal(a['reservation_usd']) for a in attempts), Decimal('0'))
    days = manifest['max_trading_days']
    pairs = manifest['pairs_per_day']
    return {'attempts_per_pair': len(attempts), 'attempts': attempts,
            'daily_attempt_cap': len(attempts) * pairs,
            'reservation_usd_ceiling_per_pair': str(per_pair),
            'experiment_usd_ceiling': str(per_pair * pairs * days)}


def select_daily_sample(envelopes: Iterable[Mapping], *, day_kst: str, max_input_age_seconds: float = 5.0,
                        pairs_per_day: int = 1, max_common_context_bytes: int | None = None) -> dict:
    """그날(KST) 제공된 봉투 중 attestation·입력 나이·크기를 통과한 첫 판단을 고른다.

    순서 = (input_available_at, decision_id). 응답·가격·결과는 보지 않는다.
    """
    date.fromisoformat(day_kst)
    offered, eligible, status, seen = [], [], {}, set()
    for env in envelopes:
        if not isinstance(env, Mapping) or not isinstance(env.get('decision_id'), str):
            continue
        try:
            if _kst_day(env.get('input_available_at')) != day_kst:
                continue
            available = _timestamp(env['input_available_at'], 'input_available_at')
        except ValueError:
            continue
        decision_id = env['decision_id']
        if decision_id in seen:
            continue  # 중복 전달은 첫 봉투만 센다
        seen.add(decision_id)
        offered.append(decision_id)
        attestation = env.get('attestation') if isinstance(env.get('attestation'), Mapping) else {}
        if attestation.get('kind') != 'in_process_nonce':
            status[decision_id] = 'unattested_input'
            continue
        try:
            received = _timestamp(env.get('received_at'), 'received_at')
        except ValueError:
            status[decision_id] = 'not_received'
            continue
        age = (received - available).total_seconds()
        if age < 0:
            status[decision_id] = 'input_clock_reversal'
        elif age > max_input_age_seconds:
            status[decision_id] = 'input_too_old'
        elif (max_common_context_bytes is not None and isinstance(env.get('common_context'), str)
              and len(env['common_context'].encode('utf-8')) > max_common_context_bytes):
            status[decision_id] = 'common_context_too_large'
        else:
            eligible.append((available, decision_id))
    eligible.sort()
    sampled = [d for _, d in eligible[:pairs_per_day]]
    for _, decision_id in eligible[pairs_per_day:]:
        status[decision_id] = 'daily_quota'
    return {'offered': offered, 'eligible': [d for _, d in eligible], 'sampled': sampled,
            'ineligible': status}


def _ms(later, earlier):
    try:
        return (_timestamp(later, 'later') - _timestamp(earlier, 'earlier')).total_seconds() * 1000
    except (ValueError, TypeError):
        return None


def _arm_summary(row, name, input_at):
    arm = row.get(name)
    if not isinstance(arm, Mapping) or not isinstance(arm.get('attempts'), list):
        return None
    replay = arm.get('sizing_replay') if isinstance(arm.get('sizing_replay'), Mapping) else {}
    evidence = arm.get('quote_evidence') if isinstance(arm.get('quote_evidence'), Mapping) else {}
    return {
        'verdict': arm.get('verdict'), 'validation_verdict': arm.get('validation_verdict'),
        'approval_basis': arm.get('approval_basis'),
        'attempts': [{k: a.get(k) for k in (
            'attempt_id', 'role', 'requested_model', 'resolved_model', 'model_identity',
            'requested_model_match', 'reasoning_effort', 'max_output_tokens', 'latency_ms',
            'request_sent', 'api_cost_usd', 'ledger_state', 'error')} for a in arm['attempts']
            if isinstance(a, Mapping)],
        'queue_delay_ms': arm.get('queue_delay_ms'),
        'provider_latency_ms': arm.get('provider_latency_ms'),
        'completion_minus_input_ms': _ms(arm.get('decision_available_at'), input_at),
        'first_quote_after_response': evidence.get('first_quote_after'),
        'quote_reasons': evidence.get('reasons'),
        'quantity': replay.get('quantity'),
        'scenario_api_cost_usd': arm.get('api_cost_usd'),
        'reasons': arm.get('reasons'),
    }


_LOOKUP_FAILED = object()


def _reconciled(lookup, attempt_id):
    """대사 비용 (값, 참조). 조회 실패는 _LOOKUP_FAILED — 대사 여부를 모르므로 비용 불명으로 센다."""
    try:
        found = lookup(attempt_id) if callable(lookup) else (lookup or {}).get(attempt_id)
    except Exception:   # guard.lookup 의 BudgetBlocked/BudgetStateError 등
        return _LOOKUP_FAILED, None
    if isinstance(found, Mapping) and isinstance(found.get('reconciled'), Mapping):
        found = found['reconciled']   # guard.lookup(attempt_id) 전체를 넘긴 경우
    if isinstance(found, Mapping) and found.get('actual_cost_usd') is not None:
        return Decimal(str(found['actual_cost_usd'])), found.get('reconciliation_id')
    return None, None


BLOCKED_SCHEMA = 'model-comparison-pair-blocked-v1'
OFFER_SCHEMA = 'comparison-offer-v1'


def offers_to_population(records: Iterable[Mapping]) -> tuple[list, list]:
    """작업자 offer 원장 레코드 → (offered_envelopes, exclusions).

    offer 기록의 봉투에 기록 received_at 을 붙여 모집단으로 쓴다(봉투 decision_id 가 기록과 다르면
    제외). rejected_*·expired·incomplete 상태(offer/transition)는 사유와 함께 제외로 보낸다.
    """
    envelopes, exclusions = [], []
    for rec in records:
        if not isinstance(rec, Mapping) or not isinstance(rec.get('decision_id'), str):
            continue
        status = rec.get('status')
        if rec.get('kind') == 'offer':
            envelope = rec.get('envelope')
            if (rec.get('schema_version') != OFFER_SCHEMA or not isinstance(envelope, Mapping)
                    or envelope.get('decision_id') != rec['decision_id']):
                continue
            envelopes.append(dict(envelope, received_at=rec.get('received_at')))
        elif rec.get('kind') != 'transition':
            continue
        if isinstance(status, str) and (status.startswith('rejected') or status in ('expired', 'incomplete')):
            reason = rec.get('reason')
            exclusions.append({'decision_id': rec['decision_id'],
                               'reason': reason if isinstance(reason, str) and reason else status})
    return envelopes, exclusions


def _run_key(index, raw):
    try:
        return (_timestamp(raw.get('requested_at'), 'requested_at').timestamp(), index)
    except ValueError:
        return (float('-inf'), index)


def build_daily_report(pair_rows: Iterable[Mapping], *, day_kst: str, offered_envelopes: Iterable[Mapping] = (),
                       exclusions: Iterable[Mapping] = (), reconciled_lookup: Callable | Mapping | None = None,
                       outcomes: Mapping | None = None, max_input_age_seconds: float = 5.0,
                       pairs_per_day: int = 1, max_common_context_bytes: int | None = None,
                       offer_records: Iterable[Mapping] = ()) -> dict:
    offered_envelopes, exclusions = list(offered_envelopes), list(exclusions)
    from_offers, excluded_offers = offers_to_population(offer_records)
    offered_envelopes += from_offers
    exclusions += excluded_offers
    selection = select_daily_sample(offered_envelopes, day_kst=day_kst,
                                    max_input_age_seconds=max_input_age_seconds, pairs_per_day=pairs_per_day,
                                    max_common_context_bytes=max_common_context_bytes)
    offered_set = set(selection['offered'])
    # 같은 판단의 여러 실행 행을 모두 보존한다(지출은 전부, 표시는 최신 행).
    runs, blocked = {}, []
    for index, raw in enumerate(pair_rows):
        if not isinstance(raw, Mapping):
            continue
        if raw.get('schema_version') == BLOCKED_SCHEMA:
            if raw.get('decision_id') in offered_set and isinstance(raw.get('reason'), str):
                blocked.append({'decision_id': raw['decision_id'], 'reason': raw['reason']})
            continue
        if raw.get('schema_version') not in PAIR_SCHEMAS:
            continue
        anchor = 'requested_at' if raw['schema_version'] == 'model-comparison-pair-v1' else raw.get('evaluation_anchor')
        try:
            if _kst_day(raw.get(anchor)) != day_kst:
                continue
        except (ValueError, TypeError):
            continue
        runs.setdefault(raw.get('decision_id'), []).append((_run_key(index, raw), raw))

    rows, day_rows = [], {}
    for decision_id, entries in runs.items():
        entries.sort(key=lambda e: e[0])
        raw = entries[-1][1]                      # 판단별 표시는 최신 실행 행
        day_rows[decision_id] = raw
        legacy = raw['schema_version'] == 'model-comparison-pair-v1'
        anchor = 'requested_at' if legacy else raw.get('evaluation_anchor')
        policy = raw.get('policy') if isinstance(raw.get('policy'), Mapping) else {}
        input_at = raw.get('input_available_at', policy.get('input_available_at'))
        outcome = (outcomes or {}).get(decision_id)
        economic = ({k: outcome.get(k) for k in ('status', 'net_delta_krw', 'evaluation_anchor', 'reasons',
                                                 'arm_reasons', 'actual_execution', 'assumptions')}
                    if isinstance(outcome, Mapping) else
                    {'status': 'not_evaluable', 'net_delta_krw': None, 'reasons': ['outcome_not_supplied'],
                     'actual_execution': False})
        rows.append({'decision_id': decision_id, 'pair_id': raw.get('pair_id'),
                     'pair_runs': len(entries), 'prior_pair_ids': [e[1].get('pair_id') for e in entries[:-1]],
                     'schema_version': raw['schema_version'],
                     'evaluation_anchor': 'requested_at (legacy)' if legacy else anchor,
                     'live_provenance': raw.get('live_provenance'), 'pricing_basis': raw.get('pricing_basis'),
                     'sampled': decision_id in selection['sampled'],
                     'input_available_at': input_at, 'enqueued_at': raw.get('enqueued_at'),
                     'requested_at': raw.get('requested_at'), 'evaluation_at': raw.get('evaluation_at'),
                     'current': _arm_summary(raw, 'current', input_at),
                     'candidate': _arm_summary(raw, 'candidate', input_at),
                     'conditional_economics': economic, 'actual_execution': False})
    all_runs = [raw for entries in runs.values() for _, raw in entries]

    # 분모별 상태: 완료 또는 하나의 사유 (차단 마커가 가장 구체적인 사유)
    excluded_by = {}
    for item in (*blocked, *exclusions):
        if isinstance(item, Mapping) and isinstance(item.get('reason'), str):
            excluded_by.setdefault(item.get('decision_id'), item['reason'])
    status = dict(selection['ineligible'])
    for decision_id in selection['sampled']:
        row = day_rows.get(decision_id)
        if row is not None and all(isinstance(row.get(a), Mapping) and row[a].get('decision_available_at')
                                   for a in ('current', 'candidate')):
            status[decision_id] = 'complete'      # 저장된 결과가 있으면 완료(평가 가능 여부는 별도)
        elif decision_id in excluded_by:
            status[decision_id] = excluded_by[decision_id]
        else:
            status[decision_id] = 'pair_result_missing' if row is None else 'partial_result'
    missing = Counter(v for d, v in status.items() if v != 'complete' and d in offered_set)
    complete = sum(v == 'complete' for v in status.values())
    stop_found = sorted({r for r in excluded_by.values() if r in STOP_REASONS}
                        | {a.get('error') for row in all_runs for arm in ('current', 'candidate')
                           if isinstance(row.get(arm), Mapping)
                           for a in row[arm].get('attempts', []) if isinstance(a, Mapping)
                           and a.get('error') in STOP_REASONS})

    # 실제 지출: 그날 모든 실행 행의 고유 attempt_id 한 번씩 (공통 Bear·재실행 중복 합산 금지).
    # 시도별 비용 = 감사 대사액 > 저장 추정액 > 불명.
    unique = {}
    for row in all_runs:
        for arm in ('current', 'candidate'):
            if isinstance(row.get(arm), Mapping):
                for a in row[arm].get('attempts', []):
                    if isinstance(a, Mapping) and isinstance(a.get('attempt_id'), str):
                        unique.setdefault(a['attempt_id'], a)
    estimated, reconciled, used = Decimal('0'), Decimal('0'), Decimal('0')
    unknown, reconciled_ids, lookup_failed = [], [], []
    for attempt_id, a in sorted(unique.items()):
        estimate = Decimal(str(a['api_cost_usd'])) if a.get('api_cost_usd') is not None else None
        if estimate is not None:
            estimated += estimate
        value, ref = _reconciled(reconciled_lookup, attempt_id) if reconciled_lookup is not None else (None, None)
        if value is _LOOKUP_FAILED:
            lookup_failed.append(attempt_id)
            unknown.append(attempt_id)
        elif value is not None:
            reconciled += value
            used += value
            reconciled_ids.append({'attempt_id': attempt_id, 'reconciliation_id': ref})
        elif estimate is not None:
            used += estimate
        else:
            unknown.append(attempt_id)
    spend = {'unique_attempts': len(unique), 'pair_runs': len(all_runs),
             'provider_sent_attempts': sum(a.get('request_sent') is True for a in unique.values()),
             'unknown_transmission_attempts': sum(a.get('request_sent') is None for a in unique.values()),
             'original_estimated_usd': str(estimated),
             'reconciled_usd': str(reconciled), 'reconciled_attempts': reconciled_ids,
             'unknown_attempts': unknown, 'lookup_failed_attempts': lookup_failed,
             'total_usd': str(used) if not unknown else None,
             'basis': 'unique attempt_id once across all pair runs of the day; '
                      'reconciled > original estimate > unknown; arm scenario costs are not summed'}
    economics = Counter(r['conditional_economics']['status'] for r in rows)
    offered_n, sampled_n = len(selection['offered']), len(selection['sampled'])
    report = {
        'schema_version': SCHEMA_VERSION, 'day_kst': day_kst, 'timezone': 'Asia/Seoul',
        'denominators': {'offered': offered_n, 'eligible': len(selection['eligible']), 'sampled': sampled_n,
                         'complete': complete, 'excluded': offered_n - complete},
        'selection_rule': 'first attested envelope by (input_available_at, decision_id) with input age '
                          f'<= {max_input_age_seconds}s; outcome-independent',
        'missing_reasons': dict(sorted(missing.items())),
        'exclusions_not_offered': sorted(str(d) for d in excluded_by if d not in selection['offered']),
        'stop_conditions_reached': stop_found,
        'spend': spend, 'rows': rows,
        'conditional_economics_summary': dict(sorted(economics.items())),
        'limitations': ['sizing_only_replay_not_executable_quantity', 'raw_market_state_not_exchange_certified',
                        'quote_depth_not_fill_vwap_or_slippage', 'not_realized_pnl_or_account_return'],
    }
    report['telegram_preview'] = (
        f"[모델비교 {day_kst}] 제공 {offered_n}·적격 {len(selection['eligible'])}·표본 {sampled_n}·"
        f"완료 {complete}·제외 {offered_n - complete}\n"
        f"지출 {'$' + str(used) if not unknown else '미확정'} (원래 추정 ${estimated}"
        + (f"·대사 ${reconciled}" if reconciled_ids else '') + ")"
        + (f" (불명 {len(unknown)}건)" if unknown else '') + "\n"
        f"조건부 경제성 계산 {economics.get('conditional_price_proxy', 0)}건 / 불가 "
        f"{len(rows) - economics.get('conditional_price_proxy', 0)}건 — 체결·실현손익 아님\n"
        f"중단 조건: {', '.join(stop_found) if stop_found else '없음'}")
    return report
