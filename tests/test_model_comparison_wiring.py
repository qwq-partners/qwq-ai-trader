"""worker 조립·생산자 현행 프로필 발행 — 합성 전송·임시 원장. 자격/네트워크/CLI 실행 없음."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analytics import model_comparison_input as mci
from src.analytics import model_comparison_wiring as w
from src.analytics import model_comparison_worker as worker_mod
from src.analytics.llm_decision_record import DecisionRecorder, set_decision_recorder
from src.analytics.model_comparison_execution import ComparisonCallGuard, ComparisonExecutionPolicy, ComparisonRequest
from src.analytics.model_comparison_recovery import inspect_pair
from src.analytics.model_comparison_runner import SingleAttemptAdapter
from src.utils.llm_budget import MonthlyBudgetLedger
from tests.test_model_comparison_claude import CLAUDE, SCRIPTS, FakeAPI, FakeCLI, _ok, reservations
from tests.test_model_comparison_current_flow import CertifiedOpenAI
from tests.test_model_comparison_input import _LLM, _cross
from tests.test_model_comparison_runner import NOW
from tests.test_model_comparison_worker import SUFFIX, envelope, wait

POLICY = {"daily_attempt_cap": 40, "max_output_tokens": 400, "allowed_models": ["gpt-5-mini", CLAUDE],
          "experiment_usd_ceiling": "100", "valid_from": "2026-01-01", "valid_until": "2027-12-31",
          "pairs_per_day": 20, "max_common_context_bytes": 65536, "required_policy_mode": "normal"}
ALL = ('openai_chat', 'claude_cli', 'anthropic_messages')


def controls():
    return mci.operating_role_controls(_LLM(), fallback_prompt_suffix=SUFFIX, fallback_timeout=10.0)


def issue(c=None, **over):
    fields = dict(experiment_id='W', fixed_at=(NOW - timedelta(hours=1)).isoformat(), horizon_seconds=900,
                  execution_policy=POLICY, candidate={'reasoning_effort': 'low'}, enabled=True)
    fields.update(over)
    return w.current_profile_manifest(controls() if c is None else c, **fields)


def transports(cli=_ok, api=_ok, owned=True, **over):
    fields = dict(openai=CertifiedOpenAI(), claude_cli=FakeCLI(cli), anthropic_api=FakeAPI(api), owned=owned)
    fields.update(over)
    return w.ComparisonTransports(**fields)


def approval(man, **over):
    fields = dict(experiment_id=man['experiment_id'], manifest_sha256=w.manifest_sha256(man), valid_from='2026-01-01',
                  valid_until='2027-12-31', transports=ALL, approved_by='synthetic-test')
    fields.update(over)
    return w.ComparisonActivationApproval(**fields)


def ledger(tmp_path):
    return MonthlyBudgetLedger(tmp_path / 'budget.db', 100, clock=lambda: NOW, policy_enabled=True)


def untouched(tr):
    return (tr.openai.calls == [] and tr.claude_cli.calls == [] and (tr.anthropic_api is None or tr.anthropic_api.calls == [])
            and not tr.claude_cli._closed and (tr.anthropic_api is None or not tr.anthropic_api._closed))


def last_offer_envelope(worker):
    files = sorted(worker.journal_dir.glob('offers-*.jsonl'))
    records = [r for f in files for r in worker_mod.read_offer_journal(f, max_bytes=1 << 24)['records'] if r.get('kind') == 'offer']
    env = dict(records[-1]['envelope'])
    env['received_at'] = records[-1]['received_at']
    return env


# ---------------------------------------------------------------- 생산자 현행 프로필 발행

def test_current_profile_manifest_freezes_producer_controls_and_matches_worker_contract(tmp_path, monkeypatch):
    c = controls()
    man = issue(c)
    assert man['review_policy'] == 'entry-independent-review-v1' and man['enabled'] is True
    assert man['current'] == c['bull'] and man['candidate'] == dict(c['bull'], reasoning_effort='low')
    assert man['bear'] == dict(c['bear'], billing_channel='subscription')
    assert man['bear_api_fallback'] == dict(c['bear'], billing_channel='api', timeout_seconds=11.5)
    assert (man['bear_internal_timeout_seconds'], man['bear_cli_timeout_seconds']) == (11.5, 5.75)
    assert man['fallback_chain'] == [] and man['strict_on_disagreement'] is False and man['bear_empty_retries'] == 0
    assert man['fallback_chain_timeout_seconds'] == 10.0 and man['empty_retries'] == 1
    assert 'resolved_model' not in json.dumps(man)                     # 요청 모델만 적는다
    assert issue(c, enabled=False)['enabled'] is False
    with pytest.raises(ValueError):
        issue(c, enabled='yes')
    assert w.manifest_sha256(man) == w.manifest_sha256(json.loads(json.dumps(man)))   # 동결 지문은 직렬화에 불변
    # worker 가 같은 생산자 제어 봉투를 수락한다(현행 프로필 대조).
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    cross = SimpleNamespace()
    tr = transports()
    worker = w.assemble_comparison_worker(cross, manifest=man, ledger=ledger(tmp_path), transports=tr, approval=approval(man),
                                          journal_dir=tmp_path / 'journal')
    try:
        assert worker.config_sha256 == w.manifest_sha256(man) and worker._controls_match(c)
        assert cross._comparison_handoff == worker.offer and cross._comparison_producer_config == worker.producer_config()
    finally:
        worker.stop(5)


@pytest.mark.parametrize('mutate,match', [
    (lambda c: {**c, 'bear': dict(c['bear'], model=None)}, 'operating_control_missing'),
    (lambda c: {**c, 'bear_transport_order': ['claude_cli']}, 'bear_transport_order'),
    (lambda c: {**c, 'bear_internal_timeout_seconds': None}, 'operating_control_missing'),
    (lambda c: {k: v for k, v in c.items() if k != 'strict_on_disagreement'}, 'operating_control_missing'),
])
def test_manifest_issuance_refuses_unsupported_or_missing_producer_values_without_substitution(mutate, match):
    with pytest.raises(ValueError, match=match):
        issue(mutate(controls()))
    with pytest.raises(ValueError, match='one_comparison_variable'):
        issue(candidate={'reasoning_effort': 'low', 'max_output_tokens': 300})
    with pytest.raises(ValueError, match='candidate'):
        issue(candidate={})


# ---------------------------------------------------------------- 조립의 fail-closed 경계

@pytest.mark.parametrize('case,match', [
    ('disabled', 'manifest_disabled'), ('sha', 'approval_manifest_mismatch'), ('experiment', 'approval_experiment_mismatch'),
    ('expired', 'approval_expired'), ('approval_transports', 'approval_transports_mismatch'),
    ('missing_api', 'transport_missing:anthropic_messages'), ('legacy', 'provider_disabled'),
])
def test_assembly_is_fail_closed_and_touches_no_transport(tmp_path, case, match):
    man = issue()
    tr = transports()
    ap = approval(man)
    if case == 'disabled':
        man = issue(enabled=False); ap = approval(man)
    elif case == 'sha':
        ap = approval(man, manifest_sha256='0' * 64)
    elif case == 'experiment':
        ap = approval(man, experiment_id='other')
    elif case == 'expired':
        ap = approval(man, valid_from='2026-01-01', valid_until='2026-01-02')
    elif case == 'approval_transports':
        ap = approval(man, transports=('openai_chat', 'claude_cli'))
    elif case == 'missing_api':
        tr = transports(anthropic_api=None)
    elif case == 'legacy':
        from tests.test_model_comparison_worker import manifest as legacy_manifest
        man = legacy_manifest(); ap = approval(man)
    cross = SimpleNamespace()
    led = ledger(tmp_path)
    with pytest.raises(ValueError, match=match):
        w.assemble_comparison_worker(cross, manifest=man, ledger=led, transports=tr, approval=ap, journal_dir=tmp_path / 'journal')
    assert not hasattr(cross, '_comparison_handoff') and untouched(tr) and reservations(led) == []


def test_import_defaults_and_env_install_create_nothing(tmp_path, monkeypatch):
    for bad in (dict(openai=SimpleNamespace(complete=lambda: None)), dict(openai=CertifiedOpenAI(), claude_cli=object()),
                dict(openai=CertifiedOpenAI(), owned='yes')):
        with pytest.raises(ValueError):
            w.ComparisonTransports(**bad)
    with pytest.raises(ValueError):
        w.ComparisonActivationApproval(experiment_id='W', manifest_sha256='zz', valid_from='2026-01-01', valid_until='2027-01-01',
                                       transports=ALL, approved_by='x')
    with pytest.raises(ValueError):
        w.ComparisonActivationApproval(experiment_id='W', manifest_sha256='0' * 64, valid_from='2026-01-01', valid_until='2027-01-01',
                                       transports=('gemini',), approved_by='x')
    # 환경변수+현행 manifest 파일만으로는 아무것도 조립되지 않는다(기존 차단 유지).
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(issue()))
    cross = SimpleNamespace()
    assert worker_mod.install_from_env(cross, env={'COMPARISON_WORKER': '1', 'COMPARISON_EXPERIMENT_MANIFEST': str(path)}) is None
    assert not hasattr(cross, '_comparison_handoff')
    with pytest.raises(ValueError, match='provider_disabled'):
        worker_mod._raw_clients()


def test_runner_assembly_routes_roles_to_declared_transports_and_disables_gemini(tmp_path):
    man = issue()
    tr = transports()
    guard = ComparisonCallGuard(ledger=ledger(tmp_path), policy=ComparisonExecutionPolicy(
        **dict(POLICY, experiment_id='W', allowed_models=tuple(POLICY['allowed_models']))))
    runner = w.build_comparison_runner(guard, man, tr)
    assert runner.adapter.openai is tr.openai and runner.adapter.gemini is None
    assert runner._claude_adapters['subscription'].client is tr.claude_cli and runner._claude_adapters['api'].client is tr.anthropic_api
    gemini_request = ComparisonRequest(model='gemini-3.1-flash-lite', prompt='p', system='s', max_output_tokens=10, temperature=0.0)
    with pytest.raises(ValueError, match='provider_disabled'):
        asyncio.run(runner.adapter(gemini_request))
    with pytest.raises(ValueError):
        SingleAttemptAdapter(openai=tr.openai, gemini=SimpleNamespace())
    assert untouched(tr)


# ---------------------------------------------------------------- 실제 생산자 발행 → worker → guard → 복구 (합성 전송)

def test_producer_envelope_flows_through_assembled_worker_to_receipts_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    recorder = DecisionRecorder(tmp_path / 'decisions', enabled=True)
    set_decision_recorder(recorder)
    cross = _cross(monkeypatch, _LLM())
    man = issue(mci.operating_role_controls(cross._llm_manager, fallback_prompt_suffix=SUFFIX, fallback_timeout=10.0))
    tr = transports(cli=SCRIPTS['timeout'])
    led = ledger(tmp_path)
    worker = w.assemble_comparison_worker(cross, manifest=man, ledger=led, transports=tr, approval=approval(man),
                                          journal_dir=tmp_path / 'journal', max_input_age_seconds=30.0)
    try:
        asyncio.run(cross.llm_second_check('005930', 'momentum', 90, {}, 'sideways', signal_id='sig-wiring'))
        decision = dict(cross.last_llm_decision)
        assert decision['comparison_handoff'] == 'accepted'            # 실제 생산자 경로(_offer_comparison)가 발행했다
        assert wait(lambda: worker.health()['finished'] == 1)
        assert len(tr.openai.calls) == 2 and len(tr.claude_cli.calls) == 1 and len(tr.anthropic_api.calls) == 1
        assert len(reservations(led)) == 3 and led.status()['unresolved_count'] == 0      # bull 2 + API 1, 구독 0
        pair_id = worker.runner.pair_id(decision['decision_id'])
        info = inspect_pair(worker.runner.guard, pair_id)
        assert info['complete'] is True and len(info['attempts']) == 4
        row = info['result']
        assert row['current']['review_policy'] == 'entry-independent-review-v1' and row['subscription_attempts'] == 1
        bear = [a for a in row['current']['attempts'] if a['role'] == 'bear']
        assert [a['billing_channel'] for a in bear] == ['subscription', 'api'] and 'bear_api_fallback_dispatched' in row['current']['reasons']
        assert row['current']['verdict'] in ('approve', 'reject') and row['policy']['review_policy'] == 'entry-independent-review-v1'
        # 저장된 같은 봉투로 다시 실행하면 저장 결과를 돌려주고 재전송은 0 (복구 경로).
        spec = worker._spec(last_offer_envelope(worker))
        assert asyncio.run(worker.runner.run(spec)) == row
        assert len(tr.claude_cli.calls) == 1 and len(tr.anthropic_api.calls) == 1 and len(tr.openai.calls) == 2
    finally:
        worker.stop(5)
        recorder.close()
        set_decision_recorder(None)
    assert tr.claude_cli._closed and tr.anthropic_api._closed                 # owned=True → 종료 시 닫힌다


def test_shared_transports_are_not_closed_on_stop_but_owned_are(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    man = issue()
    for owned in (False, True):
        tr = transports(owned=owned)
        led = ledger(tmp_path / str(owned))
        worker = w.assemble_comparison_worker(SimpleNamespace(), manifest=man, ledger=led, transports=tr, approval=approval(man),
                                              journal_dir=tmp_path / str(owned) / 'journal', max_input_age_seconds=30.0)
        try:
            at = datetime.now(timezone(timedelta(hours=9)))              # 실시간 runner 시계에 맞는 신선한 봉투
            assert worker.offer(envelope(worker, at=at, role_controls=controls())) == 'accepted'
            assert wait(lambda: worker.health()['finished'] == 1)
        finally:
            worker.stop(5)
        assert tr.claude_cli._closed is owned and tr.anthropic_api._closed is owned
        assert len(tr.claude_cli.calls) == 1 and tr.anthropic_api.calls == []
