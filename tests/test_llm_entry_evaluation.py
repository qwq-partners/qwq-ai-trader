"""LLM-window price-evaluation compatibility stays evidence-only."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "prepare_entry_evaluation.py"


def _capture(tmp_path, **kwargs):
    from test_capture_readiness import _capture as capture

    return capture(tmp_path, **kwargs)


def _run_cli(study, journal):
    return subprocess.run(
        [sys.executable, str(CLI), "--study", str(study), "--journal", str(journal),
         "--max-journal-bytes", "1048576"],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_sealed_llm_window_cli_keeps_admission_records_and_reports_no_pairs(tmp_path):
    """Validated admission/state evidence must remain passive evaluation input."""
    study, journal, _, _ = _capture(tmp_path, same_symbol=True)
    journal = Path(journal)
    before = (study.read_bytes(), journal.read_bytes())

    result = _run_cli(study, journal)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["readiness"]["status"] == "no_comparable_pairs"
    assert payload["original"]["report"]["counts"]["unknown"] == 3
    assert len(payload["review_tasks"]) == 3
    assert len({task["opportunity_id"] for task in payload["review_tasks"]}) == 3
    assert payload["readiness"]["profitability_pass"] is False
    assert payload["production_eligible"] is False
    assert payload["account_return"] is None
    assert (study.read_bytes(), journal.read_bytes()) == before


def _subscription_records(*, tr_id="H0STMKO0", status="acknowledged", symbol="900001",
                          generation=1, connection="synthetic-connection"):
    return [
        {"kind": "quote_subscription", "observed_at": "2026-10-15T00:00:00+00:00",
         "status": "connected", "generation": generation, "connection_id": connection},
        {"kind": "quote_subscription", "observed_at": "2026-10-15T00:00:01+00:00",
         "status": "requested", "generation": generation, "connection_id": connection,
         "tr_id": tr_id, "symbol": symbol},
        {"kind": "quote_subscription", "observed_at": "2026-10-15T00:00:02+00:00",
         "status": status, "generation": generation, "connection_id": connection,
         "tr_id": tr_id, "symbol": symbol},
        {"kind": "quote_subscription", "observed_at": "2026-10-15T00:00:03+00:00",
         "status": "observed", "generation": generation, "connection_id": connection,
         "tr_id": tr_id, "symbol": symbol},
        {"kind": "quote_subscription", "observed_at": "2026-10-15T00:00:04+00:00",
         "status": "unsubscribe_requested", "generation": generation, "connection_id": connection,
         "tr_id": tr_id, "symbol": symbol},
        {"kind": "quote_subscription", "observed_at": "2026-10-15T00:00:05+00:00",
         "status": "released", "generation": generation, "connection_id": connection,
         "tr_id": tr_id, "symbol": symbol},
    ]


def test_market_state_subscription_requires_explicit_capture_contract_and_physical_lifecycle():
    from src.analytics.received_entry_input import _subscription_stream_complete

    report_at = datetime.fromisoformat("2026-10-15T00:10:00+00:00")
    records = _subscription_records()
    assert not _subscription_stream_complete(records, report_at)
    assert _subscription_stream_complete(records, report_at, market_state_evidence=True)

    for defect in (
        {"status": "desired"},
        {"symbol": "*"},
        {"generation": 2},
    ):
        broken = deepcopy(records)
        broken[-1].update(defect)
        assert not _subscription_stream_complete(broken, report_at, market_state_evidence=True)

    missing_request = deepcopy(records)
    missing_request.pop(1)
    assert not _subscription_stream_complete(missing_request, report_at, market_state_evidence=True)


@pytest.mark.asyncio
async def test_actual_market_state_channel_lifecycle_is_complete_only_with_opt_in():
    from test_market_state_capture import STATUS, feed_setup, frame, receive
    from test_quote_subscription import ack
    from src.analytics.received_entry_input import _subscription_stream_complete

    feed, buffer, coordinator = await feed_setup()
    try:
        for tr_id in ("H0STCNT0", "H0STASP0", STATUS):
            await receive(feed, coordinator, json.dumps(ack((tr_id, "005930"))))
        await receive(feed, coordinator, frame(STATUS))
        coordinator.unsubscribe_success_messages = ("SYNTHETIC UNSUB ACK",)
        coordinator.end_observation()
        await coordinator.maintain()
        await receive(feed, coordinator, json.dumps(ack((STATUS, "005930"), "unsubscribe")))
        status_rows = [row for row in buffer.export()["records"]
                       if row.get("kind") == "quote_subscription" and row.get("tr_id") == STATUS]
        assert [row["status"] for row in status_rows] == [
            "requested", "acknowledged", "observed", "unsubscribe_requested", "released",
        ]
        report_at = datetime.now(timezone.utc) + timedelta(days=1)
        records = [{"kind": "scan", "candidates": [{"candidate_id": "scan:005930", "symbol": "005930"}]},
                   *buffer.export()["records"]]
        assert _subscription_stream_complete(records, report_at, market_state_evidence=True)
        assert not _subscription_stream_complete(records, report_at)
    finally:
        await feed.disconnect()


async def _priced_capture(tmp_path, monkeypatch):
    """Real writer/coordinator, synthetic market/order evidence, no live dependencies."""
    import asyncio
    from src.analytics import entry_observation as obs
    import src.analytics.entry_observation_journal as journal
    from src.analytics.entry_observation_runtime import ObservationRuntime
    from src.analytics.kis_market_state import parse_market_state
    from test_entry_capital_limits import bundle, rehash
    from test_entry_observation_runtime import load, owner, read
    from test_llm_window_observation import enter, llm_plan
    from test_market_state_capture import fields, STATUS
    from test_quote_subscription import ack
    from test_signal_window_observation import stock

    plan, context = load(tmp_path, llm_plan)
    clock = [plan.start_at - timedelta(seconds=1)]
    monkeypatch.setattr(obs, '_now', lambda: clock[0].isoformat())
    monkeypatch.setattr(journal, '_timestamp', lambda: clock[0].isoformat())
    bot = owner()
    runtime = await ObservationRuntime.install(bot, plan, now=lambda: clock[0])
    q = bot.ws_feed._quote_subscription_owner
    q.interval = 0
    sent = []

    async def send(action, key):
        sent.append((action, key))

    try:
        await q.start(send)
        clock[0] = plan.start_at
        scan = obs.capture_scan(runtime.buffer, [stock()], 'regular')
        clock[0] += timedelta(seconds=1)
        assert runtime.buffer.publish(dict(kind='rest_quote', candidate_id=f'{scan}:900001',
            observed_at=clock[0].isoformat(), requested_at=clock[0].isoformat(),
            source='KIS_FHKST01010100', source_as_of=None, quote={'high': '10200'}))
        clock[0] += timedelta(seconds=1)
        enter(runtime.buffer, scan, 'priced-signal', '900001')
        await asyncio.gather(*q._tasks)
        assert {key[0] for action, key in sent if action == 'subscribe'} == {
            'H0STASP0', 'H0STCNT0', STATUS}
        for _, key in sent:
            await q.handle_ack(ack(key), q.generation)
        provenance = {'connection_id': q.connection_id, 'generation': q.generation}
        q.note_market_state((STATUS, '900001'), q.generation)
        assert q.publish_market_state(dict(parse_market_state(STATUS, fields(STATUS, '900001')),
            kind='market_state', symbol='900001', observed_at=clock[0].isoformat(),
            received_at=clock[0].isoformat(), **provenance))
        clock[0] += timedelta(seconds=1)
        order = deepcopy(bundle()[1]['records'][3])
        order.pop('sequence')
        order.update(signal_id='priced-signal', symbol='900001', observed_at=clock[0].isoformat())
        order['capital_snapshot'].update(signal_id='priced-signal', symbol='900001',
                                         captured_at=clock[0].isoformat())
        rehash(order)
        assert runtime.buffer.publish(order)
        for name, delay, ask, bid in [('entry', 1, '10000', '9990'),
                                      ('markout', 60, '9510', '9500')]:
            clock[0] += timedelta(seconds=delay)
            q.note_data(('H0STASP0', '900001'), q.generation)
            assert runtime.buffer.publish(dict(kind='ws_quote', quote_id=name, symbol='900001',
                observed_at=clock[0].isoformat(), ask=ask, bid=bid, ask_size=100, bid_size=100,
                provenance=dict(provenance, tr_id='H0STASP0', message_count=1, source_as_of=None,
                                received_at=clock[0].isoformat())))
    finally:
        clock[0] = plan.end_at
        await runtime.close()
    return plan, context, read(plan)


@pytest.mark.asyncio
async def test_sealed_three_channel_llm_capture_requires_review_then_prices_real_order_evidence(tmp_path, monkeypatch):
    from decimal import Decimal
    from src.analytics.entry_evaluation_bundle import build_evaluation_bundle, _digest

    plan, context, observations = await _priced_capture(tmp_path, monkeypatch)
    raw = plan.study_path.read_bytes()
    journal = Path(plan.settings['journal_path'])
    before = deepcopy(observations), raw, journal.read_bytes()
    unreviewed = build_evaluation_bundle(raw, observations, study_sha256=plan.study_sha256)
    assert unreviewed['original']['capture_complete'] is True
    assert unreviewed['readiness']['status'] == 'no_comparable_pairs'
    assert set(unreviewed['review_tasks'][0]['reasons']) == {
        'MISSING_ENTRY_SESSION_REVIEW', 'MISSING_MARKOUT_SESSION_REVIEW'}
    assert unreviewed['review_tasks'][0]['entry_quote']['review_status'] == 'UNREVIEWED'
    review = {'version': 'entry-session-review-v1', 'dataset_kind': context['dataset_kind'],
              'binding': unreviewed['binding'], 'quotes': []}
    for quote in (r for r in observations['records'] if r['kind'] == 'ws_quote'):
        review['quotes'].append(dict(quote_id=quote['quote_id'], symbol=quote['symbol'],
            record_sha256=_digest(quote), session='KRX_REGULAR_CONTINUOUS',
            basis='external_event_history_review', source_ref='synthetic-reviewed-events',
            source_sha256='a' * 64, reviewer_ref='synthetic-reviewer',
            reviewed_at=plan.end_at.isoformat(), valid_from=plan.start_at.isoformat(),
            valid_until=plan.end_at.isoformat()))
    priced = build_evaluation_bundle(raw, observations, study_sha256=plan.study_sha256, session_review=review)
    assert priced['readiness']['status'] == 'complete_price_diagnostic'
    assert priced['readiness']['minimum_paired_outcomes_across_scenarios'] == 1
    assert [v['slippage_bps_each'] for v in priced['sensitivity']] == [0, 10, 30]
    losses = []
    for scenario in priced['sensitivity']:
        row = scenario['result']['report']['opportunities'][0]
        assert row['a_quantity'] == 100 and row['b_quantity'] == 0
        losses.append(Decimal(row['a_net_pnl']))
    assert losses[0] == -50000 and losses[0] > losses[1] > losses[2]
    assert priced['readiness']['profitability_pass'] is False
    assert priced['production_eligible'] is False and priced['account_return'] is None
    assert (observations, plan.study_path.read_bytes(), journal.read_bytes()) == before
    cli = _run_cli(plan.study_path, journal)
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout)['original']['capture_complete'] is True
