"""Synthetic source/date/missingness contracts for Swing supply scoring."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.signals.screener.swing_screener import SwingScreener, SwingCandidate
from src.strategies.kr.sepa_trend import SEPATrendStrategy
from src.strategies.kr.rsi2_reversal import RSI2ReversalStrategy
from src.analytics.selection_supply_policy import supply_proof

NOW = datetime(2026, 10, 20, 3, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))


def candidate(symbol='000001', **ind):
    return SwingCandidate(symbol, symbol, 'sepa_trend', 0, Decimal('10000'),
                          Decimal('9500'), Decimal('11000'), ind)


def daily(value, day='2026-10-19'):
    return {'value': value, 'source': 'FHKST01010900', 'source_date': day,
            'captured_at': NOW.isoformat(), 'status': 'observed'}


def observed(symbol, foreign, inst, day='2026-10-19'):
    return candidate(symbol, foreign_net_buy=foreign, inst_net_buy=inst,
        supply_evidence={'foreign': daily(foreign, day) if foreign is not None else None,
                         'institution': daily(inst, day) if inst is not None else None})


def live(value, symbol='000001', market='0001'):
    return {'symbol': symbol, 'net_buy_qty': value,
            '_supply_proof': supply_proof(str(value)),
            '_supply_capture': {'captured_at': NOW.isoformat(), 'market': market}}


def screener(monkeypatch, foreign=None, inst=None, estimates=None):
    sc = object.__new__(SwingScreener)
    calls = []
    class Provider:
        async def fetch_foreign_institution(self, market, investor):
            calls.append(('rank', market, investor))
            return (foreign if investor == '1' else inst) or [] if market == '0001' else []
        async def fetch_frgnmem_trade_estimate(self, **kw):
            calls.append(('estimate', kw['market']))
            return estimates or []
        async def fetch_batch_investor_daily(self, *args, **kw):
            calls.append(('daily',))
            return {}
        async def fetch_batch_valuations(self, *args):
            return {}
    sc._kis_market_data = Provider()
    return sc, calls


def freeze(monkeypatch):
    try:
        from src.analytics import swing_supply
    except ImportError:
        pass
    else:
        monkeypatch.setattr(swing_supply, 'utc_now', lambda: NOW)
    import src.signals.screener.swing_screener as module
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None) + timedelta(hours=9)
    monkeypatch.setattr(module, 'datetime', Clock)


def test_unknown_candidate_cannot_gain_lci_among_negative_peers(monkeypatch):
    freeze(monkeypatch)
    rows = [observed('000001', -100, -100), observed('000002', -200, -200), candidate('000003')]
    SwingScreener._compute_lci_zscore(None, rows)
    assert rows[2].indicators['lci'] is None
    assert rows[2].indicators.get('supply_accel') is None
    assert rows[0].indicators['market_sell_pressure'] is True


@pytest.mark.parametrize('foreign,inst', [(0, 0), (-100, -200), (100, None)])
def test_ranking_proof_preserves_zero_negative_and_missing(monkeypatch, foreign, inst):
    freeze(monkeypatch)
    sc, calls = screener(monkeypatch, [live(foreign)], [live(inst)] if inst is not None else [])
    row = candidate()
    asyncio.run(sc._apply_composite_score([row]))
    assert row.indicators['foreign_net_buy'] == foreign
    assert row.indicators['inst_net_buy'] == inst
    assert row.indicators['supply_data_age'] is None  # capture date is not provider as-of
    assert ('daily',) not in calls


def test_overlapping_estimate_not_added_and_partial_holes_do_not_fetch(monkeypatch):
    freeze(monkeypatch)
    sc, calls = screener(monkeypatch, [live(100)], [live(-50)], [live(900)])
    rows = [candidate(), candidate('000002')]
    asyncio.run(sc._apply_composite_score(rows))
    assert rows[0].indicators['foreign_net_buy'] == 100
    assert rows[1].indicators['foreign_net_buy'] is None
    assert ('daily',) not in calls
    assert rows[0].indicators['supply_coverage']['broker_estimate_rows'] == 2


def test_estimate_only_is_diagnostic_and_does_not_add_daily_calls(monkeypatch):
    freeze(monkeypatch)
    sc, calls = screener(monkeypatch, estimates=[live(900)])
    row = candidate()
    asyncio.run(sc._apply_composite_score([row]))
    assert row.indicators['foreign_net_buy'] is None
    assert ('daily',) not in calls


@pytest.mark.parametrize('second,expected', [(100, 100), (-100, None)])
def test_duplicate_ranking_rows_deduplicate_or_conflict(monkeypatch, second, expected):
    freeze(monkeypatch)
    sc, _ = screener(monkeypatch, [live(100), live(second)], [live(0)])
    row = candidate()
    asyncio.run(sc._apply_composite_score([row]))
    assert row.indicators['foreign_net_buy'] == expected


def test_legacy_cache_does_not_establish_observed_zero(monkeypatch, tmp_path):
    freeze(monkeypatch)
    path = tmp_path / '.cache/ai_trader'
    path.mkdir(parents=True)
    (path / 'supply_demand_20261019.json').write_text('{"000001":{"foreign_net_buy":0,"inst_net_buy":0}}')
    sc, _ = screener(monkeypatch)
    row = candidate()
    asyncio.run(sc._apply_composite_score([row]))
    assert row.indicators['foreign_net_buy'] is None
    assert row.indicators['supply_data_age'] is None


def test_missing_supply_gives_no_sepa_or_rsi2_fallback_bonus(monkeypatch):
    freeze(monkeypatch)
    row = candidate(supply_data_age=1, sepa_pass=True)
    sepa = SEPATrendStrategy()
    rsi = RSI2ReversalStrategy()
    zero = observed('000001', 0, 0)
    zero.indicators['sepa_pass'] = True
    assert sepa._calculate_sepa_score(zero) - sepa._calculate_sepa_score(row) == 5
    assert rsi._calculate_rsi2_score(zero) - rsi._calculate_rsi2_score(row) == 5


def test_daily_provider_raw_zero_and_missing_have_distinct_proof(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr('src.data.providers.kis_market_data.datetime', Clock)
    from test_kis_direct_instrumentation import _market, _Response
    provider, _ = _market([_Response(body={'rt_cd':'0', 'output':[
        {'stck_bsop_date':'20261019', 'frgn_ntby_qty':0, 'orgn_ntby_qty':''},
        {'stck_bsop_date':'20261016', 'frgn_ntby_qty':'-100', 'orgn_ntby_qty':'0'},
    ]})])
    async def acquire(*a, **kw): pass
    monkeypatch.setattr('src.data.providers.kis_market_data.kis_rate_limit.acquire', acquire)
    rows = asyncio.run(provider.fetch_stock_investor_daily('000001'))
    assert rows['20261019']['_supply_evidence']['foreign']['value'] == 0
    assert rows['20261019']['_supply_evidence']['institution'] is None
    assert rows['20261016']['_supply_evidence']['foreign']['value'] == -100
    assert rows['20261016']['_supply_evidence']['institution']['value'] == 0


@pytest.mark.parametrize('bad', [None, '', 'x', True, float('nan'), float('inf'), 10**13])
def test_invalid_ranking_proof_never_becomes_observed_zero(monkeypatch, bad):
    freeze(monkeypatch)
    row = live(0)
    row['_supply_proof'] = supply_proof(bad)
    sc, _ = screener(monkeypatch, [row], [live(0)])
    item = candidate()
    asyncio.run(sc._apply_composite_score([item]))
    assert item.indicators['foreign_net_buy'] is None


def test_lci_requires_same_date_and_source_and_clears_old_acceleration(monkeypatch):
    freeze(monkeypatch)
    rows = [observed('000001', 100, 100), observed('000002', -100, -100)]
    rows[0].indicators['supply_evidence']['institution']['source_date'] = '2026-10-16'
    rows[0].indicators['supply_accel'] = 0.5
    SwingScreener._compute_lci_zscore(None, rows)
    assert [c.indicators['lci'] for c in rows] == [None, None]
    assert rows[0].indicators['supply_accel'] is None


def test_incompatible_positive_sides_cannot_gain_double_bonus(monkeypatch):
    freeze(monkeypatch)
    row = observed('000001', 100, 100)
    row.indicators['sepa_pass'] = True
    row.indicators['supply_evidence']['institution']['source_date'] = '2026-10-16'
    single = observed('000001', 100, None)
    single.indicators['sepa_pass'] = True
    assert SEPATrendStrategy()._calculate_sepa_score(row) == SEPATrendStrategy()._calculate_sepa_score(single)


def test_versioned_cache_roundtrip_keeps_source_date_and_unknown_side(monkeypatch, tmp_path):
    freeze(monkeypatch)
    sc, _ = screener(monkeypatch)
    proofs = {'000001': {'foreign': daily(0, '2026-10-16'), 'institution': None}}
    sc._save_swing_supply_cache(proofs, NOW)
    loaded = sc._load_swing_supply_cache(NOW + timedelta(days=1))
    assert loaded['000001']['foreign']['source_date'] == '2026-10-16'
    assert loaded['000001']['institution'] is None
    assert not list((tmp_path / '.cache/ai_trader').glob('supply_demand_*.json'))


def test_expired_live_cache_cannot_become_dated_history(monkeypatch):
    freeze(monkeypatch)
    sc, _ = screener(monkeypatch, [live(100)], [live(0)])
    row = candidate()
    asyncio.run(sc._apply_composite_score([row]))
    assert sc._load_swing_supply_cache(NOW + timedelta(seconds=601)) == {}


def test_holiday_age_uses_actual_source_date(monkeypatch):
    freeze(monkeypatch)
    from src.analytics import swing_supply as policy
    at = datetime(2026, 10, 12, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(policy, 'utc_now', lambda: at)
    monkeypatch.setattr(policy, 'is_kr_market_holiday', lambda day: day.isoformat() == '2026-10-09')
    row = observed('000001', 0, 0, '2026-10-08')
    for proof in row.indicators['supply_evidence'].values():
        proof['captured_at'] = at.isoformat()
    assert policy.supply_age(row.indicators) == 1
    assert policy.previous_session(at.date()).isoformat() == '2026-10-08'


@pytest.mark.parametrize('reverse', [False, True])
def test_sepa_freshness_relaxation_is_candidate_specific(monkeypatch, reverse):
    freeze(monkeypatch)
    import datetime as clock_module
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr(clock_module, 'datetime', Clock)
    dated = observed('000001', 0, 0)
    missing = candidate('000002', supply_data_age=2)
    for row in (dated, missing):
        row.indicators['atr_14'] = 5.0
    strategy = SEPATrendStrategy()
    # Isolate admission thresholds at a fixed technical score: default70, datedT-1=65.
    monkeypatch.setattr(strategy, '_calculate_sepa_score', lambda c: 66)
    rows = [dated, missing]
    if reverse:
        rows.reverse()
    result = asyncio.run(strategy.generate_batch_signals(rows))
    assert [s.symbol for s in result] == ['000001']


def test_daily_duplicate_conflict_does_not_choose_last_row(monkeypatch):
    from test_kis_direct_instrumentation import _market, _Response
    provider, _ = _market([_Response(body={'rt_cd':'0', 'output':[
        {'stck_bsop_date':'20261016', 'frgn_ntby_qty':'100', 'orgn_ntby_qty':'0'},
        {'stck_bsop_date':'20261016', 'frgn_ntby_qty':'-100', 'orgn_ntby_qty':'0'},
        {'stck_bsop_date':'20261016', 'frgn_ntby_qty':'100', 'orgn_ntby_qty':'0'},
    ]})])
    async def acquire(*a, **kw): pass
    monkeypatch.setattr('src.data.providers.kis_market_data.kis_rate_limit.acquire', acquire)
    rows = asyncio.run(provider.fetch_stock_investor_daily('000001'))
    assert rows['20261016']['_supply_evidence']['foreign'] is None
    assert rows['20261016']['_supply_evidence']['institution']['value'] == 0


def test_fetch_capture_after_request_start_remains_valid(monkeypatch):
    freeze(monkeypatch)
    from src.analytics import swing_supply as policy
    clock = [NOW]
    monkeypatch.setattr(policy, 'utc_now', lambda: clock[0])
    sc, _ = screener(monkeypatch)
    async def fetch(market, investor):
        clock[0] = NOW + timedelta(seconds=1)
        row = live(100 if investor == '1' else 0)
        row['_supply_capture']['captured_at'] = clock[0].isoformat()
        return [row] if market == '0001' else []
    sc._kis_market_data.fetch_foreign_institution = fetch
    row = candidate()
    asyncio.run(sc._apply_composite_score([row]))
    assert row.indicators['foreign_net_buy'] == 100
    assert row.indicators['inst_net_buy'] == 0


def test_previous_session_survives_long_holiday_closure(monkeypatch):
    from src.analytics import swing_supply as policy
    at = datetime(2026, 10, 20, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(policy, 'is_kr_market_holiday', lambda day: '2026-10-10' <= day.isoformat() <= '2026-10-19')
    row = observed('000001', 0, -100, '2026-10-09')
    assert policy.valid_evidence(row.indicators['supply_evidence']['foreign'], at) is not None
    assert policy.supply_age(row.indicators, at) == 1


@pytest.mark.parametrize('day', ['2026101', '20261032', '2026-10-16', '', None])
def test_daily_evidence_requires_exact_valid_provider_date(day):
    from src.analytics.swing_supply import daily_evidence
    assert daily_evidence('0', day, NOW.isoformat()) is None


def test_daily_duplicate_missing_then_valid_does_not_erase_conflict(monkeypatch):
    from test_kis_direct_instrumentation import _market, _Response
    provider, _ = _market([_Response(body={'rt_cd':'0', 'output':[
        {'stck_bsop_date':'20261016'},
        {'stck_bsop_date':'20261016', 'frgn_ntby_qty':'100', 'orgn_ntby_qty':'0'},
    ]})])
    async def acquire(*a, **kw): pass
    monkeypatch.setattr('src.data.providers.kis_market_data.kis_rate_limit.acquire', acquire)
    rows = asyncio.run(provider.fetch_stock_investor_daily('000001'))
    assert rows['20261016']['_supply_evidence'] == {'foreign': None, 'institution': None}


@pytest.mark.parametrize('change', [
    {'captured_at': '2026-10-20T03:00:00'},
    {'captured_at': '2026-10-20T03:00:01+00:00'},
    {'source_date': '20261019'},
    {'source_date': '2026-10-21'},
    {'source_date': '2026-10-18'},
    {'source': 'unknown'},
])
def test_invalid_cached_provenance_stays_unknown(change):
    from src.analytics.swing_supply import valid_evidence
    proof = {**daily(0), **change}
    assert valid_evidence(proof, NOW) is None


@pytest.mark.parametrize('change', [
    {'market': '0002'},
    {'captured_at': '2026-10-20T03:00:00'},
    {'captured_at': (NOW - timedelta(seconds=601)).isoformat()},
])
def test_invalid_live_capture_stays_unknown(change):
    from src.analytics.swing_supply import ranking_evidence
    row = live(0)
    row['_supply_capture'].update(change)
    assert ranking_evidence([('0001', [row])], NOW) == {'000001': None}


def test_expired_monotonic_proof_cannot_be_refreshed_by_capture():
    import time
    from src.analytics.swing_supply import ranking_evidence
    row = live(100)
    row['_supply_proof']['observed_monotonic'] = time.monotonic() - 601
    assert ranking_evidence([('0001', [row])], NOW) == {'000001': None}


def test_daily_fallback_uses_exact_day_and_refreshes_response_clock(monkeypatch):
    freeze(monkeypatch)
    from src.analytics import swing_supply as policy
    clock = [NOW]
    monkeypatch.setattr(policy, 'utc_now', lambda: clock[0])
    sc, calls = screener(monkeypatch)
    async def fetch(symbols, target, **kw):
        calls.append(('daily', target))
        clock[0] += timedelta(seconds=1)
        exact = {**daily(0), 'captured_at': clock[0].isoformat()}
        old = {**daily(10, '2026-10-16'), 'captured_at': clock[0].isoformat()}
        return {'000001': {'_supply_evidence': {'foreign': exact, 'institution': old}}}
    sc._kis_market_data.fetch_batch_investor_daily = fetch
    row = candidate()
    asyncio.run(sc._apply_composite_score([row]))
    assert row.indicators['foreign_net_buy'] == 0
    assert row.indicators['inst_net_buy'] is None
    assert calls.count(('daily', '20261019')) == 1


@pytest.mark.parametrize('capture', ['', '2026-10-20T03:00:00', None])
def test_provider_proof_rejects_malformed_or_naive_capture(capture):
    from src.analytics.swing_supply import daily_evidence
    assert daily_evidence(0, '20261019', capture) is None
