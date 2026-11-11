"""현행 독립 검토 판정·Bear CLI→API 상위 흐름 — 실제 임시 월 원장, 가짜 전송. 자격/네트워크/CLI 실행 없음."""
import asyncio
from dataclasses import replace
import json

import pytest

from src.analytics import model_comparison_recovery as recovery
from src.analytics import model_comparison_runner as m
from src.analytics.model_comparison_claude import ClaudeSingleAttemptAdapter
from src.analytics.model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy, ComparisonRequest
from src.utils.llm_budget import BudgetBlocked, MonthlyBudgetLedger
from tests.test_model_comparison_claude import CLAUDE, SCRIPTS, FakeAPI, FakeCLI, _ok, _sent, bear_controls, reservations
from tests.test_model_comparison_runner import NOW, FakeGemini, FakeOpenAI

CURRENT = 'entry-independent-review-v1'
LEGACY = 'legacy-single-opinion-v1'


def _empty(response):
    response = _ok(response)
    response.content = ''
    return response


class CertifiedOpenAI(FakeOpenAI):
    """현행 정책용 Bull: 공급자 보고 모델이 요청과 정확히 같다(기본). resolved 로 snapshot/불일치/미확인을 흉내 낸다."""
    def __init__(self, resolved='exact', **kw):
        super().__init__(**kw)
        self.resolved = resolved

    async def complete(self, prompt, system='', model=None, **kw):
        response = await super().complete(prompt, system, model, **kw)
        response.resolved_model = model if self.resolved == 'exact' else self.resolved
        return response


def guard_for(tmp_path, *, daily_attempt_cap=40):
    ledger = MonthlyBudgetLedger(tmp_path / 'budget.db', 100, clock=lambda: NOW, policy_enabled=True)
    return ledger, ComparisonCallGuard(ledger=ledger, policy=ComparisonExecutionPolicy(
        experiment_id='F', daily_attempt_cap=daily_attempt_cap, allowed_models=('gpt-5-mini', CLAUDE, 'gemini-3.1-flash-lite'),
        max_output_tokens=400, experiment_usd_ceiling='100', valid_from='2026-01-01', valid_until='2027-12-31',
        pairs_per_day=20, max_common_context_bytes=65536))


def current_spec(**over):
    fields = dict(decision_id='decision-cf', signal_id='signal-1', symbol='005490', common_context='same immutable facts',
                  input_available_at=NOW.isoformat(),
                  current=m.RoleControls('gpt-5-mini', 400, 'minimal', 0.3, 20260803, 12.0),
                  candidate=m.RoleControls('gpt-5-mini', 400, 'low', 0.3, 20260803, 12.0),
                  bear=bear_controls('subscription', timeout_seconds=12.0), bear_empty_retries=0,
                  review_policy=CURRENT, bear_internal_timeout_seconds=11.5, bear_cli_timeout_seconds=5.75,
                  bear_api_fallback=bear_controls('api', timeout_seconds=11.5),
                  neutral_sizing=None, pricing_basis='offline_replay', fixed_at=NOW.isoformat(),
                  strict_on_disagreement=False, fallback_chain_timeout_seconds=10.0)
    fields.update(over)
    return m.PairSpec(**fields)


def setup(tmp_path, *, cli=_ok, api=_ok, cli_delay=0.0, api_delay=0.0, bull=None, spec=None, cli_channel='subscription',
          daily_attempt_cap=40):
    ledger, guard = guard_for(tmp_path, daily_attempt_cap=daily_attempt_cap)
    op, ge = (bull if bull is not None else CertifiedOpenAI()), FakeGemini()
    cli_client = (FakeAPI if cli_channel == 'api' else FakeCLI)(cli, delay=cli_delay)
    api_client = FakeAPI(api, delay=api_delay)
    runner = m.PairedComparisonRunner(
        guard=guard, adapter=m.SingleAttemptAdapter(openai=op, gemini=ge, clock=lambda: NOW), clock=lambda: NOW,
        claude_adapter=ClaudeSingleAttemptAdapter(cli_client, clock=lambda: NOW),
        claude_api_adapter=ClaudeSingleAttemptAdapter(api_client, clock=lambda: NOW))
    return ledger, guard, runner, (spec if spec is not None else current_spec()), op, cli_client, api_client


def bear_attempts(row, arm='current'):
    return [a for a in row[arm]['attempts'] if a['role'] == 'bear']


# ---------------------------------------------------------------- 현행 판정 정책

def test_current_policy_decides_only_with_both_valid_reviews(tmp_path):
    ledger, guard, runner, spec, op, cli, api = setup(tmp_path)
    row = asyncio.run(runner.run(spec))
    # FakeOpenAI: minimal→REJECT(current), low→APPROVE(candidate); Bear→REJECT. strict False: 하나라도 긍정이면 approve.
    assert (row['current']['verdict'], row['candidate']['verdict']) == ('reject', 'approve')
    for arm in ('current', 'candidate'):
        a = row[arm]
        assert a['review_policy'] == CURRENT and a['approval_basis'] == 'entry_independent_review_v1'
        assert a['policy_verdict'] == a['verdict'] and a['validation_verdict'] == a['verdict']
        assert a['bear_verdict'] is False and len(bear_attempts(row, arm)) == 1
        assert 'independent_review_unavailable' not in a['reasons'] and 'explicit_fallback_policy_missing' not in a['reasons']
    assert api.calls == [] and len(cli.calls) == 1


@pytest.mark.parametrize('case', ['bull_empty', 'bear_empty', 'bear_identity_mismatch', 'bear_refusal', 'bear_cli_timeout_api_incomplete'])
def test_current_policy_holds_when_either_review_is_missing_or_invalid(tmp_path, case):
    kwargs = {}
    if case == 'bull_empty':
        kwargs['bull'] = CertifiedOpenAI(empty=True)
    elif case == 'bear_empty':
        kwargs['cli'] = _empty
    elif case == 'bear_identity_mismatch':
        kwargs['cli'] = SCRIPTS['identity_mismatch']
    elif case == 'bear_refusal':
        kwargs['cli'] = SCRIPTS['refusal']
    else:
        kwargs['cli'], kwargs['api'] = SCRIPTS['timeout'], SCRIPTS['incomplete']
    ledger, guard, runner, spec, op, cli, api = setup(tmp_path, **kwargs)
    row = asyncio.run(runner.run(spec))
    for arm in ('current', 'candidate'):
        a = row[arm]
        assert a['verdict'] == 'hold' and a['policy_verdict'] == 'hold' and a['validation_verdict'] == 'hold'
        assert 'independent_review_unavailable' in a['reasons']
        assert a['sizing_replay'] is None and a['unavailable_policy'] == 'hold_blocks_entry'
        assert 'explicit_fallback_policy_missing' not in a['reasons']   # 현행 정책에는 단일 모델 폴백이 없다
        if case == 'bull_empty':
            assert a['bear_verdict'] is False and a['bull_verdict'] is None
        else:
            assert a['bear_verdict'] is None
    # 거절·신원 불일치·빈 응답은 API 대체 대상이 아니고, 허용 실패(cli_timeout)만 API 1회다.
    assert len(api.calls) == (1 if case == 'bear_cli_timeout_api_incomplete' else 0)


@pytest.mark.parametrize('resolved,decided', [('exact', True), ('gpt-5-mini-2025-08-07', True),
                                              ('provider-reported-version', False), (None, False)])
def test_current_policy_counts_bull_opinion_only_with_certified_identity(tmp_path, resolved, decided):
    ledger, guard, runner, spec, op, cli, api = setup(tmp_path, bull=CertifiedOpenAI(resolved))
    row = asyncio.run(runner.run(spec))
    arm = row['candidate']                                  # bull APPROVE, bear REJECT
    assert arm['policy_verdict'] == 'approve'               # 파싱된 의견만의 판정은 그대로 남는다
    if decided:
        assert arm['verdict'] == 'approve' and arm['bull_verdict'] is True
        assert 'resolved_model_unverified_or_mismatch' not in arm['reasons']   # 공식 snapshot 은 인증이자 진단에서도 일치
    else:
        assert arm['verdict'] == 'hold' and arm['bull_verdict'] is None
        assert 'bull_identity_unverified_or_mismatch' in arm['reasons'] and arm['sizing_replay'] is None
    assert api.calls == []


def test_current_profile_without_api_fallback_still_applies_cli_and_internal_deadlines(tmp_path):
    spec = current_spec(bear_api_fallback=None)
    ledger, guard, runner, _, op, cli, api = setup(tmp_path, cli=SCRIPTS['timeout'], spec=spec)
    row = asyncio.run(runner.run(spec))
    (call,) = cli.calls
    assert 0 < call['deadline'] - call['loop_time'] <= 5.75 and api.calls == []
    assert row['current']['verdict'] == 'hold' and 'bear_api_fallback_not_configured' in row['current']['reasons']
    assert len(bear_attempts(row)) == 1 and 'bear_api_fallback' not in spec.snapshot()


def test_legacy_policy_rows_keep_single_opinion_semantics_and_shape(tmp_path):
    from tests.test_model_comparison_runner import setup as legacy_setup
    _, ledger, runner, spec, op, ge = legacy_setup(tmp_path)
    assert spec.review_policy == LEGACY
    row = asyncio.run(runner.run(spec))
    assert 'review_policy' not in row['current'] and 'policy_verdict' not in row['current']
    snapshot = spec.snapshot()
    assert not {'review_policy', 'bear_internal_timeout_seconds', 'bear_cli_timeout_seconds', 'bear_api_fallback'} & set(snapshot)


# ---------------------------------------------------------------- CLI→API 상위 흐름

def test_cli_success_never_dispatches_api_and_keeps_api_reservations_to_bulls(tmp_path):
    ledger, guard, runner, spec, op, cli, api = setup(tmp_path)
    row = asyncio.run(runner.run(spec))
    assert len(cli.calls) == 1 and api.calls == []
    assert len(reservations(ledger)) == 2 and row['subscription_attempts'] == 1
    bear = bear_attempts(row)
    assert [a['attempt_id'].rsplit(':', 1)[1] for a in bear] == ['0'] and bear[0]['billing_channel'] == 'subscription'


@pytest.mark.parametrize('kind', ['cli_timeout', 'cli_incomplete', 'cli_rate_limited', 'cli_auth_unavailable', 'cli_unavailable'])
def test_authorized_cli_failure_dispatches_api_once_with_separate_receipts(tmp_path, kind):
    script = SCRIPTS['not_sent'] if kind == 'cli_unavailable' else _sent(kind)
    ledger, guard, runner, spec, op, cli, api = setup(tmp_path, cli=script)
    row = asyncio.run(runner.run(spec))
    assert len(cli.calls) == 1 and len(api.calls) == 1
    bear = bear_attempts(row)
    assert [a['attempt_id'] for a in bear] == [f"{row['pair_id']}:bear:0", f"{row['pair_id']}:bear:1"]
    assert (bear[0]['billing_channel'], bear[0]['transport'], bear[0]['success']) == ('subscription', 'claude_cli', False)
    assert bear[0]['transport_error_kind'] == kind and bear[0]['api_cost_usd'] is None
    assert (bear[1]['billing_channel'], bear[1]['transport'], bear[1]['success']) == ('api', 'anthropic_messages', True)
    assert bear[1]['api_cost_usd'] is not None and bear[1]['reservation_token'] is not None
    assert len(reservations(ledger)) == 3 and all(s == 'settled' for (s,) in reservations(ledger))   # bull 2 + API 1
    assert ledger.status()['unresolved_count'] == 0
    assert row['subscription_attempts'] == 1 and row['admitted_attempts'] == 4
    assert row['current']['bear_verdict'] is False and row['current']['verdict'] == 'reject'     # 최종 의견은 API 1회만
    assert row['candidate']['verdict'] == 'approve'
    assert 'bear_api_fallback_dispatched' in row['current']['reasons']
    assert asyncio.run(runner.run(spec)) == row and len(api.calls) == 1                           # 복구는 재전송 0


@pytest.mark.parametrize('kind', ['refusal', 'identity_mismatch', 'policy_violation'])
def test_forbidden_cli_outcomes_never_fall_back_to_api(tmp_path, kind):
    ledger, guard, runner, spec, op, cli, api = setup(tmp_path, cli=_sent(kind, **({'model_identity_status': 'mismatch'} if kind == 'identity_mismatch' else {})))
    row = asyncio.run(runner.run(spec))
    assert len(cli.calls) == 1 and api.calls == []
    assert len(bear_attempts(row)) == 1 and row['current']['verdict'] == 'hold'
    assert 'bear_api_fallback_not_authorized' in row['current']['reasons']
    assert len(reservations(ledger)) == 2


def test_guard_block_before_cli_dispatch_is_not_an_api_fallback_trigger(tmp_path):
    # 같은 Bear CLI attempt_id 가 이미 전송 전 차단으로 영속돼 있으면 가드가 BudgetBlocked 를 올린다. 가드 차단은 대체 사유가 아니다.
    ledger, guard, runner, spec, op, cli, api = setup(tmp_path)
    pair_id = runner.pair_id(spec.decision_id)
    stale = ComparisonRequest(model=CLAUDE, prompt='x', system='', max_output_tokens=10, reasoning_effort='low',
                              temperature=None, billing_channel='subscription')
    with pytest.raises(BudgetBlocked, match='comparison_adapter_channel_mismatch'):
        asyncio.run(guard.call(stale, dispatch=ClaudeSingleAttemptAdapter(FakeAPI(), clock=lambda: NOW), attempt_id=f'{pair_id}:bear:0'))
    row = asyncio.run(runner.run(spec))
    bear = bear_attempts(row)
    assert len(bear) == 1 and bear[0]['error'] in ('comparison_adapter_channel_mismatch', 'comparison_attempt_mismatch')
    assert cli.calls == [] and api.calls == []
    assert 'bear_api_fallback_not_authorized' in row['current']['reasons'] and row['current']['verdict'] == 'hold'
    assert (bear[0]['api_cost_usd'], bear[0]['request_sent']) == ('0', False)       # 전송 전 차단만 0원


def short_panel(seconds, **over):
    """공통 패널 시한을 줄인 현행 spec(Bull/Bear 역할 시한 동일)."""
    fields = dict(current=m.RoleControls('gpt-5-mini', 400, 'minimal', 0.3, 20260803, seconds),
                  candidate=m.RoleControls('gpt-5-mini', 400, 'low', 0.3, 20260803, seconds),
                  bear=bear_controls('subscription', timeout_seconds=seconds))
    fields.update(over)
    return current_spec(**fields)


def test_role_timeout_during_cli_leaves_timeout_attempt_and_no_api(tmp_path):
    spec = short_panel(0.3, bear_internal_timeout_seconds=0.25, bear_cli_timeout_seconds=0.1,
                       bear_api_fallback=bear_controls('api', timeout_seconds=0.25))
    ledger, guard, runner, _, op, cli, api = setup(tmp_path, cli_delay=1.0, spec=spec)
    row = asyncio.run(runner.run(spec))
    bear = bear_attempts(row)
    assert len(bear) == 1 and bear[0]['error'] == 'TimeoutError' and api.calls == []
    assert bear[0]['request_sent'] is None and bear[0]['billing_channel'] == 'subscription'
    assert row['current']['verdict'] == 'hold' and ledger.status()['unresolved_count'] == 0


def test_exhausted_internal_budget_skips_api_instead_of_extending_time(tmp_path):
    spec = current_spec(bear=bear_controls('subscription', timeout_seconds=12.0), bear_internal_timeout_seconds=0.25,
                        bear_cli_timeout_seconds=0.1, bear_api_fallback=bear_controls('api', timeout_seconds=0.25))
    ledger, guard, runner, _, op, cli, api = setup(tmp_path, cli=SCRIPTS['timeout'], cli_delay=0.3, spec=spec)
    row = asyncio.run(runner.run(spec))
    assert len(cli.calls) == 1 and api.calls == []
    assert 'bear_api_fallback_skipped_time_exhausted' in row['current']['reasons'] and row['current']['verdict'] == 'hold'


def test_api_fallback_receives_only_remaining_internal_time(tmp_path):
    ledger, guard, runner, spec, op, cli, api = setup(tmp_path, cli=SCRIPTS['timeout'], cli_delay=0.2)
    row = asyncio.run(runner.run(spec))
    (cli_call,), (api_call,) = cli.calls, api.calls
    cli_budget = cli_call['deadline'] - cli_call['loop_time']
    api_budget = api_call['deadline'] - api_call['loop_time']
    assert 0 < cli_budget <= 5.75                                   # CLI 는 min(8, 내부/2)
    assert 0 < api_budget <= 11.5 - 0.2 + 0.02 and api_budget < 11.5   # API 는 내부 예산에서 CLI 경과를 뺀 나머지
    assert api_call['loop_time'] >= cli_call['loop_time'] + 0.2
    bear = bear_attempts(row)
    assert bear[0]['latency_ms'] is not None and bear[1]['latency_ms'] is not None   # 시도별 시간은 따로 남는다
    assert bear[1]['requested_at'] >= bear[0]['responded_at']


def test_admission_latency_cannot_extend_the_bear_internal_deadline(tmp_path, monkeypatch):
    import time
    ledger, guard, runner, spec, op, cli, api = setup(tmp_path, cli=SCRIPTS['timeout'], cli_delay=0.2)
    original = guard._admit

    def slow_admit(attempt_id, request_json):          # 가드 입장(원장 쓰기)에 걸리는 시간을 흉내 낸다 — CLI·API 양쪽
        if attempt_id.endswith((':bear:0', ':bear:1')):
            time.sleep(0.15)
        return original(attempt_id, request_json)
    monkeypatch.setattr(guard, '_admit', slow_admit)
    asyncio.run(runner.run(spec))
    (cli_call,), (api_call,) = cli.calls, api.calls
    # CLI 상한은 체인 시작 기준 5.75초다: 입장 0.15초 뒤 호출됐으므로 남은 상대 시한은 5.75-0.15 를 넘지 못한다.
    assert cli_call['deadline'] - cli_call['loop_time'] <= 5.75 - 0.15 + 0.02
    # 내부 예산은 Bear 체인 시작에서 고정된 절대 시한이다. CLI 호출 시각은 시작 이후이므로 API 시한은 그보다 11.5초 뒤를 넘지 못한다.
    assert api_call['deadline'] <= cli_call['loop_time'] + 11.5 - 0.15
    assert api_call['deadline'] - api_call['loop_time'] < 11.5 - 0.15 - 0.2 - 0.15 + 0.02


@pytest.mark.parametrize('role', ['bear', 'bull_current'])
def test_result_arriving_inside_deadline_but_settled_after_it_is_not_counted(tmp_path, monkeypatch, role):
    import time
    if role == 'bear':   # Bear 내부 예산만 짧게(패널 0.6). 동기 sleep 은 루프 전체를 멈추므로 역할별로 한쪽만 좁힌다
        spec = short_panel(0.6, bear_internal_timeout_seconds=0.3, bear_cli_timeout_seconds=0.1,
                           bear_api_fallback=bear_controls('api', timeout_seconds=0.3))
    else:                # 패널 0.3, Bear 체인은 그 안에서 즉시 끝난다
        spec = short_panel(0.3, bear_internal_timeout_seconds=0.3, bear_cli_timeout_seconds=0.1,
                           bear_api_fallback=bear_controls('api', timeout_seconds=0.3))
    ledger, guard, runner, _, op, cli, api = setup(tmp_path, cli=SCRIPTS['timeout'], spec=spec)
    original = guard._store_result

    def slow_store(attempt_id, token, payload):       # 응답 수신 뒤 저장·정산에 걸리는 동기 시간을 흉내 낸다
        if attempt_id.endswith(':bear:1' if role == 'bear' else ':bull_current:0'):
            time.sleep(0.35)
        return original(attempt_id, token, payload)
    monkeypatch.setattr(guard, '_store_result', slow_store)
    row = asyncio.run(runner.run(spec))
    arm = row['current']
    assert arm['verdict'] == 'hold' and role.split('_')[0] + '_role_deadline_exceeded_after_response' in arm['reasons']
    assert arm['policy_verdict'] != 'hold'                      # 의견 자체는 파싱됐다 — 시한 때문에 채택되지 않았을 뿐
    late = [a for a in arm['attempts'] if a['attempt_id'].endswith(':bear:1' if role == 'bear' else ':bull_current:0')][0]
    assert late['success'] is True and late['ledger_state'] == 'known'      # 영수증·정산은 보존된다
    assert ledger.status()['unresolved_count'] == 0
    other = row['candidate']
    # 공유 Bear 가 늦으면 두 arm 모두 hold. Bull 사례에서도 동기 저장 지연이 루프를 멈춰 candidate 의 Bull 역할 벽시계가
    # 0.3초를 넘기므로 같은 사유로 hold 다 — 역할 전체 벽시계 재검사의 의도된 의미(운영 whole-call deadline).
    assert other['verdict'] == 'hold'
    assert (role.split('_')[0] + '_role_deadline_exceeded_after_response') in other['reasons']


def test_common_panel_deadline_is_fixed_before_roles_start(tmp_path, monkeypatch):
    # 패널 20초. Bear 입장(동기)이 10초 걸려 Bull 역할 시작이 밀리고, Bull 저장에 11초가 더 걸리면 Bull 은 자기 역할
    # 시작 기준 11초 만에 끝났어도(자기 시한 30초) 패널 시작 기준 21초라 운영처럼 늦은 의견이다.
    # 벽시계 sleep 대신 runner·어댑터·asyncio 타이머가 함께 읽는 루프 시계(loop.time())를 전진시킨다 — 실제 대기 0.
    # 시계는 '실제 단조 시계 + 전진분'이다(완전 고정 시계는 타이머만 남는 순간 루프가 멈출 수 있다). 따라서 Bull 지연은
    # 전진분만으로 결정되고, Bear 내부 시한(19초)은 실행 지터 9초까지 흡수한다(11/9 CI: 0.4초 시한·0.1초 여유 플레이크).
    spec = short_panel(20.0, bear_internal_timeout_seconds=19.0, bear_cli_timeout_seconds=8.0,
                       bear_api_fallback=bear_controls('api', timeout_seconds=19.0))
    ledger, guard, runner, _, op, cli, api = setup(tmp_path, spec=spec)
    admit, store = guard._admit, guard._store_result
    skew = [0.0]

    def slow_admit(attempt_id, request_json):
        if attempt_id.endswith(':bear:0'):
            skew[0] += 10.0
        return admit(attempt_id, request_json)

    def slow_store(attempt_id, token, payload):
        if ':bull_' in attempt_id:
            skew[0] += 11.0
        return store(attempt_id, token, payload)
    monkeypatch.setattr(guard, '_admit', slow_admit)
    monkeypatch.setattr(guard, '_store_result', slow_store)

    def loop_factory():
        loop = asyncio.new_event_loop()
        real = loop.time
        loop.time = lambda: real() + skew[0]
        return loop
    with asyncio.Runner(loop_factory=loop_factory) as run:
        row = run.run(runner.run(spec))
    for arm in ('current', 'candidate'):
        assert row[arm]['verdict'] == 'hold'
        assert 'bull_role_deadline_exceeded_after_response' in row[arm]['reasons']
        assert 'bear_role_deadline_exceeded_after_response' not in row[arm]['reasons']
        bull = [a for a in row[arm]['attempts'] if a['role'].startswith('bull_')][-1]
        assert bull['success'] is True and bull['ledger_state'] == 'known'   # 영수증은 보존된다


def test_identity_certification_does_not_default_an_explicit_empty_transport():
    assert m._identity_certified({'requested_model_match': False, 'provider': 'openai', 'requested_model': 'gpt-5-mini',
                                  'resolved_model': 'gpt-5-mini-2025-08-07'}) is True
    assert m._identity_certified({'requested_model_match': False, 'provider': 'openai', 'transport': '',
                                  'requested_model': 'gpt-5-mini', 'resolved_model': 'gpt-5-mini-2025-08-07'}) is False
    assert m._identity_certified({'requested_model_match': False, 'provider': 'openai', 'requested_model': 'gpt-5-mini',
                                  'resolved_model': None}) is False


def test_bull_arms_share_one_bear_chain_and_each_attempt_cost_is_preserved(tmp_path):
    ledger, guard, runner, spec, op, cli, api = setup(tmp_path, cli=SCRIPTS['timeout'])
    row = asyncio.run(runner.run(spec))
    assert len(cli.calls) == 1 and len(api.calls) == 1                 # 두 arm 이 같은 Bear 체인을 공유
    assert bear_attempts(row, 'current') == bear_attempts(row, 'candidate')
    ids = {a['attempt_id'] for arm in ('current', 'candidate') for a in row[arm]['attempts']}
    assert len(ids) == 4 and row['total_comparison_cost_usd'] is not None   # bull 2 + API 1 만 API 합계
    assert row['current']['api_cost_usd'] is not None and row['current']['api_cost_scope'].startswith('scenario_cost')


@pytest.mark.parametrize('cli_script', [_ok, SCRIPTS['timeout']])
def test_current_flow_rows_survive_storage_failure_and_recovery_recomputes_identically(tmp_path, monkeypatch, cli_script):
    from contextlib import contextmanager
    import sqlite3
    ledger, guard, runner, spec, op, cli, api = setup(tmp_path, cli=cli_script)
    original = ledger._connect

    class Connection:
        def __init__(self, db): self.db = db
        def execute(self, sql, *args):
            if sql.startswith('UPDATE comparison_pairs SET result='):
                raise sqlite3.OperationalError('injected pair storage failure')
            return self.db.execute(sql, *args)
        def __getattr__(self, name): return getattr(self.db, name)

    @contextmanager
    def connect(**kwargs):
        with original(**kwargs) as db:
            yield Connection(db)
    monkeypatch.setattr(ledger, '_connect', connect)
    with pytest.raises(m.PairResultPersistenceError) as error:
        asyncio.run(runner.run(spec))
    monkeypatch.setattr(ledger, '_connect', original)
    row = error.value.result
    attempts = recovery._row_attempts(row, row['pair_id'])
    arms = [dict(row[name]) for name in ('current', 'candidate')]
    assert m.finalize_row(row['pair_id'], spec.snapshot(), row['requested_at'], *arms) == row
    for attempt in attempts.values():
        recovery._verify_attempt(guard, spec.snapshot(), row['pair_id'], attempt)
    assert len(attempts) == (3 if cli_script is _ok else 4)


# ---------------------------------------------------------------- 프로필 규칙·스냅샷

@pytest.mark.parametrize('mutate,match', [
    (lambda s: replace(s, fallback_prompt='p', fallback_chain=(m.RoleControls('gpt-5.6-sol', 100, None, 0.3, None, 10.0),)), 'single_model_fallback'),
    (lambda s: replace(s, strict_on_disagreement=True), 'strict_on_disagreement'),
    (lambda s: replace(s, bear=m.RoleControls('gemini-3.1-flash-lite', 400, None, 0.0, timeout_seconds=12.0), bear_api_fallback=None,
                       bear_cli_timeout_seconds=None, bear_internal_timeout_seconds=None), 'claude_bear'),
    (lambda s: replace(s, bear_internal_timeout_seconds=None), 'internal_timeout'),   # cli 시한이 내부 예산을 요구
    (lambda s: replace(s, bear_internal_timeout_seconds=12.5), 'bear_internal_timeout'),
    (lambda s: replace(s, bear_cli_timeout_seconds=None), 'bear_cli_timeout'),
    (lambda s: replace(s, bear_cli_timeout_seconds=6.0), 'bear_cli_timeout'),          # > min(8, 11.5/2)
    (lambda s: replace(s, bear_api_fallback=bear_controls('subscription', timeout_seconds=11.5)), 'bear_api_fallback'),
    (lambda s: replace(s, bear_api_fallback=bear_controls('api', timeout_seconds=12.0)), 'bear_api_fallback'),
    (lambda s: replace(s, bear_api_fallback=m.RoleControls('gpt-5-mini', 400, 'low', 0.3, None, 11.5)), 'bear_api_fallback'),
    (lambda s: replace(s, bear_api_fallback=bear_controls('api', timeout_seconds=11.5, max_output_tokens=100)), 'bear_api_fallback'),
    (lambda s: replace(s, bear=bear_controls('api', timeout_seconds=12.0)), 'cli_primary'),
    (lambda s: replace(s, review_policy='something-else'), 'review_policy'),
    (lambda s: replace(s, bear=bear_controls('subscription', timeout_seconds=11.9), bear_api_fallback=bear_controls('api', timeout_seconds=11.5)), 'common_panel_timeout'),
])
def test_current_profile_rules(mutate, match):
    spec = current_spec()
    spec.validate()
    with pytest.raises(ValueError, match=match):
        mutate(spec).validate()


def test_current_snapshot_carries_profile_and_legacy_snapshot_is_unchanged(tmp_path):
    spec = current_spec()
    snapshot = spec.snapshot()
    assert snapshot['review_policy'] == CURRENT and snapshot['bear_internal_timeout_seconds'] == 11.5
    assert snapshot['bear_cli_timeout_seconds'] == 5.75 and snapshot['bear_api_fallback']['billing_channel'] == 'api'
    assert 'billing_channel' not in snapshot['current'] and snapshot['bear']['billing_channel'] == 'subscription'
    json.dumps(snapshot)
    legacy = replace(spec, review_policy=LEGACY, bear_api_fallback=None, bear_cli_timeout_seconds=None,
                     bear_internal_timeout_seconds=None)
    legacy.validate()
    assert not {'review_policy', 'bear_internal_timeout_seconds', 'bear_cli_timeout_seconds', 'bear_api_fallback'} & set(legacy.snapshot())


def test_runner_requires_channel_matching_adapters_before_claim(tmp_path):
    ledger, guard = guard_for(tmp_path)
    adapter = m.SingleAttemptAdapter(openai=FakeOpenAI(), gemini=FakeGemini(), clock=lambda: NOW)
    cli, api = ClaudeSingleAttemptAdapter(FakeCLI(), clock=lambda: NOW), ClaudeSingleAttemptAdapter(FakeAPI(), clock=lambda: NOW)
    with pytest.raises(ValueError, match='claude_adapter_channel'):
        m.PairedComparisonRunner(guard=guard, adapter=adapter, claude_adapter=cli, claude_api_adapter=cli)
    runner = m.PairedComparisonRunner(guard=guard, adapter=adapter, claude_adapter=cli, clock=lambda: NOW)
    with pytest.raises(ValueError, match='claude_api_adapter_required'):
        asyncio.run(runner.run(current_spec()))
    assert runner.admitted_pair_ids(ledger.today().isoformat()) == set()
    report = m.report_pairs([], payloads=[], ledger_status={})
    assert 'hold' in report['summary']['current_verdicts']


# ---------------------------------------------------------------- worker: 프로필 검증·선택 주입·활성화 거부

def current_manifest(**kw):
    from tests.test_model_comparison_worker import manifest
    bear = dict(model=CLAUDE, max_output_tokens=400, reasoning_effort='low', temperature=None, seed=None,
                timeout_seconds=12.0, billing_channel='subscription')
    value = manifest(bear=bear, fallback_chain=[], strict_on_disagreement=False, review_policy=CURRENT,
                     bear_empty_retries=0, bear_internal_timeout_seconds=11.5, bear_cli_timeout_seconds=5.75,
                     bear_api_fallback=dict(bear, billing_channel='api', timeout_seconds=11.5))
    value['current'] = dict(value['current'], timeout_seconds=12.0)
    value['candidate'] = dict(value['candidate'], timeout_seconds=12.0)
    value['execution_policy'] = dict(value['execution_policy'], allowed_models=['gpt-5-mini', CLAUDE])
    value.update(kw)
    return value


def current_role_controls(man):
    return {'bull': man['current'], 'bear': man['bear'], 'fallback_chain': [], 'fallback_chain_timeout_seconds': man['fallback_chain_timeout_seconds'],
            'fallback_prompt_suffix': '', 'fallback_system': '', 'empty_retries': man['empty_retries'],
            'bear_empty_retries': 0, 'bear_internal_timeout_seconds': 11.5,
            'bear_transport_order': ['claude_cli', 'anthropic_messages'], 'strict_on_disagreement': False}


def test_worker_accepts_envelope_built_from_real_operating_role_controls(tmp_path, monkeypatch):
    from pathlib import Path
    from src.analytics import model_comparison_input as mci
    from src.utils.llm import LLMProvider
    from tests.test_model_comparison_worker import envelope, wait
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)

    class Manager:   # 생산자가 읽는 최소 표면: light 모델명만. 네트워크·자격 없음.
        def _get_model(self, provider, weight):
            return CLAUDE if provider == LLMProvider.ANTHROPIC else 'gpt-5.6-terra'
    controls = mci.operating_role_controls(Manager(), fallback_prompt_suffix='\nYES/NO', fallback_timeout=10.0)
    # L02(2026-11-11): 운영 Bull 은 gpt-5.6-terra/none — worker 의 현행 manifest 도 같은 프로필이어야 봉투를 받는다.
    cur = dict(model='gpt-5.6-terra', max_output_tokens=400, reasoning_effort='none', temperature=0.3, seed=20260803, timeout_seconds=12.0)
    terra_manifest = current_manifest(current=cur, candidate=dict(cur, reasoning_effort='low'))
    terra_manifest['execution_policy'] = dict(terra_manifest['execution_policy'], allowed_models=['gpt-5.6-terra', CLAUDE])
    # L01 Task 2(2026-11-09): 생산자는 운영 순서(API 1차)를 적고, CLI→API 만 구현한 현행 worker 는 그 봉투를 거부한다(fail-closed).
    assert controls['bear_transport_order'] == ['anthropic_messages', 'claude_cli'] and 'billing_channel' not in controls['bear']
    worker, ledger, op, cli, api = build_current_worker(tmp_path, terra_manifest, cli=SCRIPTS['timeout'])
    worker.start()
    try:
        assert worker.offer(envelope(worker, role_controls=controls)) == 'rejected_control_mismatch'
        legacy = dict(controls, bear_transport_order=['claude_cli', 'anthropic_messages'])
        assert worker.offer(envelope(worker, 'dec-legacy', role_controls=legacy)) == 'accepted'
        assert wait(lambda: worker.health()['finished'] == 1)
    finally:
        worker.stop(5)
    assert len(cli.calls) == 1 and len(api.calls) == 1 and len(reservations(ledger)) == 3


def build_current_worker(tmp_path, man, *, cli=_ok, api=_ok):
    from src.analytics import model_comparison_worker as w
    from tests.test_model_comparison_worker import Clock
    ledger = MonthlyBudgetLedger(tmp_path / 'budget.db', 100, clock=lambda: NOW, policy_enabled=True)
    policy = dict(man['execution_policy'], experiment_id=man['experiment_id'])
    policy['allowed_models'] = tuple(policy['allowed_models'])
    guard = ComparisonCallGuard(ledger=ledger, policy=ComparisonExecutionPolicy(**policy))
    op, cli_client, api_client = CertifiedOpenAI(), FakeCLI(cli), FakeAPI(api)
    runner = m.PairedComparisonRunner(guard=guard, adapter=m.SingleAttemptAdapter(openai=op, gemini=FakeGemini(), clock=lambda: NOW),
                                      claude_adapter=ClaudeSingleAttemptAdapter(cli_client, clock=lambda: NOW),
                                      claude_api_adapter=ClaudeSingleAttemptAdapter(api_client, clock=lambda: NOW), clock=lambda: NOW)
    worker = w.ComparisonWorker(runner, manifest=man, clock=Clock())
    return worker, ledger, op, cli_client, api_client


def test_worker_validates_current_profile_at_init_before_any_transport(tmp_path):
    from src.analytics import model_comparison_worker as w
    build_current_worker(tmp_path, current_manifest())            # 올바른 프로필은 생성된다
    for i, bad in enumerate((dict(bear_cli_timeout_seconds=6.0), dict(strict_on_disagreement=True),
                             dict(fallback_chain=[dict(model='gpt-5.6-sol', max_output_tokens=100, reasoning_effort=None, temperature=0.3, seed=None, timeout_seconds=10.0)]),
                             dict(bear_api_fallback=dict(current_manifest()['bear_api_fallback'], billing_channel='subscription')),
                             dict(bear_api_fallback=dict(current_manifest()['bear_api_fallback'], max_output_tokens=100)))):
        with pytest.raises(ValueError):
            build_current_worker(tmp_path / f'bad{i}', current_manifest(**bad))
    with pytest.raises(ValueError, match='provider_disabled'):
        w._raw_clients()                                            # 실제 클라이언트 생성 경로는 여전히 닫혀 있다


def test_worker_with_injected_synthetic_adapters_runs_cli_then_api_chain_and_rejects_mismatched_controls(tmp_path, monkeypatch):
    from pathlib import Path
    from tests.test_model_comparison_worker import envelope, wait
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    man = current_manifest()
    worker, ledger, op, cli, api = build_current_worker(tmp_path, man, cli=SCRIPTS['timeout'])
    worker.start()
    try:
        controls = current_role_controls(man)
        assert worker.offer(envelope(worker, role_controls=controls)) == 'accepted'
        assert wait(lambda: worker.health()['finished'] == 1)
        assert len(cli.calls) == 1 and len(api.calls) == 1 and len(op.calls) == 2
        stale = dict(controls, bear_transport_order=['claude_cli'])
        assert worker.offer(envelope(worker, 'dec-2', role_controls=stale)) == 'rejected_control_mismatch'
        legacy = dict(controls, bear_internal_timeout_seconds=12.0)
        assert worker.offer(envelope(worker, 'dec-3', role_controls=legacy)) == 'rejected_control_mismatch'
    finally:
        worker.stop(5)
    assert len(cli.calls) == 1 and len(api.calls) == 1
    assert len(reservations(ledger)) == 3
    assert cli._closed and api._closed                       # 종료 시 주입된 Claude 전송도 닫힌다


def test_install_from_env_with_current_manifest_still_refuses_activation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.analytics import model_comparison_worker as w
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(current_manifest()))
    created = []
    original = w._raw_clients

    def spy():
        created.append(1)
        return original()
    monkeypatch.setattr(w, '_raw_clients', spy)
    cross = SimpleNamespace()
    assert w.install_from_env(cross, env={'COMPARISON_WORKER': '1', 'COMPARISON_EXPERIMENT_MANIFEST': str(path)}) is None
    assert not hasattr(cross, '_comparison_handoff')
