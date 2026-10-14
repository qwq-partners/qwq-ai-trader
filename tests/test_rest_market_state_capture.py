"""추가 요청 없는 현재가 상태 보존과 명시 관측 계약의 통합 회귀."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.analytics import entry_observation as obs
from src.execution.broker import kis_kr
from test_entry_observation_runtime import load
from test_llm_window_observation import llm_plan

START = datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)


def raw_output():
    return dict(stck_prpr='310000', stck_oprc='309000', stck_hgpr='312000',
                stck_lwpr='308000', stck_sdpr='309000', acml_vol='100',
                prdy_vrss='1000', prdy_ctrt='0.32', stck_shrn_iscd='005490',
                iscd_stat_cls_code='55', temp_stop_yn='N', vi_cls_code='N', ovtm_vi_cls_code='N',
                ignored_sensitive_field='DO_NOT_COPY')


async def get_quote(monkeypatch, output=None, *, rt_cd='0'):
    broker = object.__new__(kis_kr.KISBroker)
    broker.config = SimpleNamespace(base_url='https://synthetic.invalid')
    monkeypatch.setattr(kis_kr.KISBroker, 'is_connected', property(lambda self: True))
    clock = [START]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0] if tz is not None else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(kis_kr, 'datetime', Clock)
    calls = []
    async def request(url, tr_id, params):
        calls.append((url, tr_id, params))
        clock[0] += timedelta(seconds=2)
        return {'rt_cd': rt_cd, 'output': raw_output() if output is None else output}
    broker._api_get = request
    result = await broker.get_quote('005490')
    assert calls == [('https://synthetic.invalid/uapi/domestic-stock/v1/quotations/inquire-price',
                      'FHKST01010100', {'fid_cond_mrkt_div_code':'J', 'fid_input_iscd':'005490'})]
    return result


@pytest.mark.asyncio
async def test_existing_quote_preserves_state_and_logical_call_bounds_without_more_requests(monkeypatch):
    result = await get_quote(monkeypatch)
    assert result['price'] == 310000 and result['volume'] == 100
    assert 'market_state_snapshot' in result
    snap = result['market_state_snapshot']
    assert snap['request_started_at'] == START.isoformat()
    assert snap['response_available_at'] == (START+timedelta(seconds=2)).isoformat()
    assert snap['temp_stop_yn'] == 'N' and snap['response_symbol'] == '005490'
    assert snap['source_as_of'] is None and snap['state_verdict'] == 'UNKNOWN'
    assert 'DO_NOT_COPY' not in str(snap)


def enabled_plan(context):
    llm_plan(context)
    context['capture']['rest_market_state_evidence'] = True


def test_rest_state_opt_in_is_valid_explicit_llm_contract(tmp_path):
    plan, _ = load(tmp_path, enabled_plan)
    assert plan.settings['rest_market_state_evidence'] is True


@pytest.mark.asyncio
async def test_optional_snapshot_failure_preserves_existing_price(monkeypatch):
    from src.analytics import kis_rest_market_state as state
    def broken(*args, **kwargs):
        raise ValueError('synthetic metadata failure')
    monkeypatch.setattr(state, 'build_snapshot', broken)
    result = await get_quote(monkeypatch)
    assert result['price'] == 310000 and 'market_state_snapshot' not in result
    assert await get_quote(monkeypatch, rt_cd='1') == {}


@pytest.mark.parametrize('value', [False, 1, 'true', None])
def test_opt_in_rejects_implicit_truthy_and_disabled_values(tmp_path, value):
    def change(c):
        enabled_plan(c)
        c['capture']['rest_market_state_evidence'] = value
    with pytest.raises(ValueError):
        load(tmp_path, change)


@pytest.mark.asyncio
async def test_legacy_capture_ignores_additive_quote_metadata(tmp_path, monkeypatch):
    from src.analytics.entry_observation_runtime import _WindowBuffer
    from test_signal_window_observation import stock
    plan, _ = load(tmp_path, llm_plan)
    b = _WindowBuffer(plan, lambda: START+timedelta(seconds=3))
    monkeypatch.setattr(obs, '_now', lambda: (START+timedelta(seconds=3)).isoformat())
    sid = obs.capture_scan(b, [stock('005490')], 'regular')
    quote = await get_quote(monkeypatch)
    obs.capture_rest_quote(b, sid, '005490', quote)
    row = b.export()['records'][-1]
    assert row['kind'] == 'rest_quote' and 'market_state_snapshot' not in row
    assert row['quote']['price'] == 310000


@pytest.mark.asyncio
async def test_quote_to_opted_journal_to_strict_report_is_raw_only(tmp_path, monkeypatch, capsys):
    import importlib.util
    import json
    import sys
    from pathlib import Path
    from src.analytics.entry_observation_runtime import ObservationRuntime, validate_window_contract
    from src.analytics.entry_observation_journal import read_observation_journal
    from test_entry_observation_runtime import NOW, owner
    from test_signal_window_observation import stock
    plan, context = load(tmp_path, enabled_plan)
    clock = [NOW]
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    runtime = await ObservationRuntime.install(owner(), plan, now=lambda: clock[0])
    clock[0] = START
    sid = obs.capture_scan(runtime.buffer, [stock('005490')], 'regular')
    quote = await get_quote(monkeypatch)
    clock[0] = START+timedelta(seconds=2)
    obs.capture_rest_quote(runtime.buffer, sid, '005490', quote)
    quote['market_state_snapshot']['vi_cls_code'] = 'Y'  # 복사 후 원 객체 변조 격리
    clock[0] += timedelta(seconds=2)
    runtime.buffer.publish(dict(kind='ws_quote', symbol='005490', quote_id='q', observed_at=clock[0].isoformat(),
        ask='310100', ask_size=10, bid='310000', bid_size=10,
        provenance={'received_at':clock[0].isoformat(), 'connection_id':'ws-1', 'generation':1}))
    clock[0] = plan.end_at
    await runtime.close()
    ledger = read_observation_journal(plan.settings['journal_path'], max_bytes=10_000_000,
                                     expected_study_sha256=plan.study_sha256)
    assert ledger['complete']
    validate_window_contract(context, ledger)
    raw = next(r for r in ledger['records'] if r['kind'] == 'rest_quote')
    assert raw['market_state_snapshot']['vi_cls_code'] == 'N'
    without_opt_in = json.loads(json.dumps(context))
    without_opt_in['capture'].pop('rest_market_state_evidence')
    with pytest.raises(ValueError, match='REST'):
        validate_window_contract(without_opt_in, ledger)
    decisions = tmp_path/'decisions.jsonl'
    decisions.write_text(json.dumps(dict(schema_version='llm-decision-v1',decision_id='d',symbol='005490',
        decision_available_at=(START+timedelta(seconds=3)).isoformat(),result={'approved':True}))+'\n')
    path = Path(__file__).resolve().parents[1]/'scripts/report_decision_quotes.py'
    spec = importlib.util.spec_from_file_location('report_rest_quote_e2e',path)
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    monkeypatch.setattr(sys,'argv',[str(path),'--decisions',str(decisions),'--ledger',plan.settings['journal_path'],
        '--study-sha256',plan.study_sha256])
    assert cli.main() == 0
    report = json.loads(capsys.readouterr().out)
    rest = report['rows'][0]['rest_market_state_at_quote']
    assert rest['raw_available'] is True and rest['state_verdict'] == 'UNKNOWN'
    assert rest['usable_for_market_state'] is False
    assert rest['age_upper_bound_seconds'] == 4
    assert report['summary']['rest_raw_available'] == 1
    assert report['rows'][0]['evaluable'] is False and 'market_state_unknown' in report['rows'][0]['reasons']


@pytest.mark.parametrize('bad_snapshot', [{'not': 'a snapshot'}, 'invalid', None])
def test_bad_or_missing_optional_snapshot_does_not_drop_price(tmp_path, monkeypatch, bad_snapshot):
    from src.analytics.entry_observation_runtime import _WindowBuffer
    from test_signal_window_observation import stock
    plan, _ = load(tmp_path, enabled_plan)
    b = _WindowBuffer(plan, lambda: START)
    monkeypatch.setattr(obs, '_now', lambda: START.isoformat())
    sid = obs.capture_scan(b, [stock('005490')], 'regular')
    obs.capture_rest_quote(b, sid, '005490', {'price': 100, 'market_state_snapshot': bad_snapshot})
    row = b.export()['records'][-1]
    assert row['quote']['price'] == 100 and 'market_state_snapshot' not in row
    assert row['rest_market_state_status'] == ('missing_snapshot' if bad_snapshot is None else 'invalid_snapshot')


def test_non_llm_capture_cannot_opt_in(tmp_path):
    with pytest.raises(ValueError):
        load(tmp_path, lambda c: c['capture'].update(rest_market_state_evidence=True))


def test_report_normalizes_quote_timezone_and_counts_rest_exclusions_separately():
    from src.analytics.kis_rest_market_state import build_snapshot
    from src.analytics.decision_quote_join import join_decisions, summarize
    qtime = START+timedelta(seconds=4)
    snapshot = build_snapshot('005490', raw_output(), request_started_at=START.isoformat(),
                              response_available_at=(START+timedelta(seconds=2)).isoformat())
    payloads = [dict(kind='rest_quote', candidate_id='scan:005490', observed_at=(START+timedelta(seconds=2)).isoformat(),
                    market_state_snapshot=snapshot, rest_market_state_status='recorded'),
                dict(kind='ws_quote', symbol='005490', observed_at=qtime.isoformat(), ask='100', ask_size=1,
                     provenance={'received_at':qtime.astimezone(timezone(timedelta(hours=9))).isoformat(),
                                 'connection_id':'c1', 'generation':1})]
    decision = dict(symbol='005490', decision_available_at=(START+timedelta(seconds=3)).isoformat())
    ledger = {'verified':True, 'complete':True}
    rows = join_decisions([decision], payloads, ledger_status=ledger)
    assert rows[0]['rest_market_state_at_quote']['raw_available'] is True
    payloads[0].pop('market_state_snapshot')
    rows = join_decisions([decision, dict(decision, symbol='000660')], payloads, ledger_status=ledger)
    summary = summarize(rows, ledger_status=ledger)
    assert summary['rest_raw_available'] == 0
    assert summary['rest_raw_unavailable'] == 2
    assert summary['rest_diagnostic_reasons'] == {'latest_snapshot_missing':1, 'no_associated_quote':1}
    assert all(not r['evaluable'] for r in rows)
