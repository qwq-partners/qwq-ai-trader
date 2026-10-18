"""Missing ranking evidence must not become an observed no-buying penalty."""
from dataclasses import asdict
from datetime import timedelta
import pytest
from test_selection_source_basis import screener, stock, run
from test_selection_modifiers import provider_fixture
from src.signals.screener.kr_screener import StockScreener


def native_screener(monkeypatch, raw='MISSING', change=5):
    provider, calls = provider_fixture(monkeypatch, raw)
    s = screener(monkeypatch, {'screen_volume_surge': [stock(change_pct=change)]})
    s._kis_market_data = provider
    s.min_trading_value = 1e10  # supply rows filtered out, evidence still matters
    for name in ('screen_foreign_buying', 'screen_institutional_buying', '_apply_spdi_filter'):
        setattr(s, name, getattr(StockScreener, name).__get__(s))
    return s, provider, calls


@pytest.mark.parametrize('raw', ['MISSING', '', 'x', True, float('nan'), float('inf')])
@pytest.mark.parametrize('sentiment, expected', [(None, 50), ('bearish', 45)])
def test_unknown_raw_supply_keeps_only_bearish_baseline_penalty(monkeypatch, raw, sentiment, expected):
    s, _, calls = native_screener(monkeypatch, raw)
    result = run(s, overnight_sentiment=sentiment)
    assert result[0].score == expected
    assert len(calls) == 4


@pytest.mark.parametrize('raw,change,expected', [('0', 3, 35), ('-1', 4.99, 35), ('0', 5, 30), ('-1', 8, 30), ('1', 5, 50)])
def test_observed_nonpositive_has_one_price_penalty_owner(monkeypatch, raw, change, expected):
    s, _, _ = native_screener(monkeypatch, raw, change)
    result = run(s)
    assert result[0].score == expected
    assert sum('SPDI↓' in r or '개인단독매수' in r for r in result[0].reasons) <= 1


def test_scan_evidence_is_independent_of_optional_observers(monkeypatch):
    s, _, _ = native_screener(monkeypatch, '0')
    plain = run(s)
    observed = run(s, capture_selection=True, capture_selection_sources=True, capture_selection_modifiers=True)
    a, b = asdict(plain[0]), asdict(observed[0])
    a.pop('selection_basis'); b.pop('selection_basis')
    assert a == b
    assert plain[0].score == 30


def test_supply_cache_preserves_evidence_without_additional_calls(monkeypatch):
    s, _, calls = native_screener(monkeypatch, '0')
    assert run(s)[0].score == 30
    assert run(s)[0].score == 30
    assert len(calls) == 4


def test_next_scan_failure_does_not_reuse_expired_supply_evidence(monkeypatch):
    s, provider, _ = native_screener(monkeypatch, '0')
    assert run(s)[0].score == 30
    s._cache_time = {k: v - timedelta(hours=1) for k, v in s._cache_time.items()}
    async def fail(**kw):
        raise RuntimeError('synthetic fetch failure')
    provider.fetch_foreign_institution = fail
    assert run(s, overnight_sentiment='bearish')[0].score == 45


def test_normalized_legacy_provider_cache_cannot_establish_zero(monkeypatch):
    s, provider, _ = native_screener(monkeypatch, '0')
    from datetime import datetime
    for market in ('0001', '0002'):
        for investor in ('1', '2'):
            key = f'foreign_inst_{market}_{investor}'
            provider._cache[key] = [dict(symbol='000001', net_buy_qty=0)]
            provider._cache_ts[key] = datetime.now()
    assert run(s, overnight_sentiment='bearish')[0].score == 45


@pytest.mark.parametrize('investor', ['1', '2'])
def test_partial_failure_or_absent_other_side_is_unknown(monkeypatch, investor):
    s, provider, _ = native_screener(monkeypatch, '0')
    fetch = provider.fetch_foreign_institution
    async def partial(**kw):
        if kw['investor'] == investor:
            return []  # ranking miss, not observed zero
        return await fetch(**kw)
    provider.fetch_foreign_institution = partial
    assert run(s, overnight_sentiment='bearish')[0].score == 45


def test_filtered_positive_still_prevents_absence_and_supports_bearish_defense(monkeypatch):
    s, _, calls = native_screener(monkeypatch, '1')
    result = run(s, overnight_sentiment='bearish')
    assert result[0].score == 45
    assert not result[0].has_foreign_buying and not result[0].has_inst_buying
    assert len(calls) == 4


@pytest.mark.parametrize('raw', ['0', '1'])
def test_evidence_absolute_age_is_not_renewed_by_layered_cache(monkeypatch, raw):
    from src.analytics import selection_supply_policy as policy
    now = [1000.0]
    monkeypatch.setattr(policy.time, 'monotonic', lambda: now[0])
    s, provider, calls = native_screener(monkeypatch, raw)
    assert run(s, overnight_sentiment='bearish')[0].score == (10 if raw == '0' else 45)
    now[0] = 1601.0
    # Leave both cache layers populated: proof's absolute age still wins.
    assert run(s, overnight_sentiment='bearish')[0].score == 45
    assert len(calls) == 4


def test_failed_scan_does_not_leak_previous_positive(monkeypatch):
    s, provider, _ = native_screener(monkeypatch, '1')
    assert run(s, overnight_sentiment='bearish')[0].score == 45
    s._cache_time = {k: v - timedelta(hours=1) for k, v in s._cache_time.items()}
    async def fail(**kw):
        raise RuntimeError('synthetic fetch failure')
    provider.fetch_foreign_institution = fail
    assert run(s, overnight_sentiment='bearish')[0].score == 45


@pytest.mark.parametrize('raw', [True, False, '', 'x', float('nan'), float('inf'), 1.5, '1000000000001'])
def test_invalid_raw_cannot_create_positive_supply_candidate(monkeypatch, raw):
    s, _, _ = native_screener(monkeypatch, raw)
    s.min_trading_value = 0
    result = run(s)
    assert result[0].score == 50
    assert not result[0].has_foreign_buying and not result[0].has_inst_buying


@pytest.mark.parametrize('overflow', [False, True])
def test_conflict_or_evidence_overflow_never_establishes_absence(monkeypatch, overflow):
    from src.analytics.selection_supply_policy import supply_proof
    s, provider, _ = native_screener(monkeypatch, '0')
    fetch = provider.fetch_foreign_institution
    async def contradictory(**kw):
        rows = await fetch(**kw)
        if overflow:
            rows = rows + [dict(rows[0], symbol=f'{i:06d}') for i in range(2, 515)]
        return rows + [dict(rows[0], _supply_proof=supply_proof('1'))]
    provider.fetch_foreign_institution = contradictory
    assert run(s)[0].score == 50


@pytest.mark.parametrize('method', ['__init__', 'raw', 'end'])
def test_observer_failure_cannot_disable_real_nonpositive_penalty(monkeypatch, method):
    from src.analytics.selection_modifiers import ModifierCapture
    s, _, calls = native_screener(monkeypatch, '0')
    def fail(*args):
        raise RuntimeError('synthetic observer failure')
    monkeypatch.setattr(ModifierCapture, method, fail)
    result = run(s, capture_selection=True, capture_selection_modifiers=True)
    assert result[0].score == 30
    assert len(calls) == 4
    assert result[0].selection_basis['version'] == 'kr-selection-basis-v1'


def test_cached_decision_proof_does_not_claim_legacy_unknown_shadow_comparison(monkeypatch):
    s, _, calls = native_screener(monkeypatch, '0')
    assert run(s)[0].score == 30
    result = run(s, capture_selection=True, capture_selection_modifiers=True)
    basis = result[0].selection_basis
    assert result[0].score == 30
    assert next(stage for stage in basis['modifier_stages'] if stage['stage_id'] == 'spdi')['delta'] == -20
    assert basis['same_input_shadow']['status'] == 'inputs_incomplete'
    assert basis['same_input_shadow']['shadow_delta_vs_actual'] is None
    assert len(calls) == 4


@pytest.mark.parametrize('raw, expected', [(' +1 ', 1), (' 0\n', 0), ('\t-1 ', -1)])
def test_provider_preserves_whitespace_padded_integer_proof(monkeypatch, raw, expected):
    import asyncio
    provider, calls = provider_fixture(monkeypatch, raw)
    rows = asyncio.run(provider.fetch_foreign_institution(market='0001', investor='1'))
    assert rows[0]['net_buy_qty'] == expected
    assert rows[0]['_supply_proof']['value'] == expected
    assert len(calls) == 1


@pytest.mark.parametrize('raw, malformed', [('x', True), (True, True), ('1,000', True), ('1.0', True), ('', True), ('MISSING', False)])
def test_provider_marks_present_invalid_quantity_schema_without_inventing_evidence(monkeypatch, raw, malformed):
    import asyncio
    from src.analytics.selection_source_status import SourceCapture
    from src.data.providers import kis_market_data
    provider, calls = provider_fixture(monkeypatch, raw)
    messages = []
    monkeypatch.setattr(kis_market_data.logger, 'warning', messages.append)
    capture = SourceCapture()
    rows = asyncio.run(capture.call('kis_foreign_buying', provider.fetch_foreign_institution,
                                   market='0001', investor='1'))
    source = next(row for row in capture.wrap(rows).selection_sources['runs']
                  if row['source_id'] == 'kis_foreign_buying')
    assert rows[0]['net_buy_qty'] == 0 and rows[0]['_supply_proof'] is None
    assert source['failure_count'] == int(malformed)
    assert source['failure_reason'] == ('schema' if malformed else None)
    assert len(messages) == int(malformed)
    assert len(calls) == 1


def test_many_invalid_rows_emit_one_capped_warning_and_schema_marker(monkeypatch):
    import asyncio
    from src.analytics.selection_source_status import SourceCapture
    from src.data.providers import kis_market_data
    provider, calls = provider_fixture(monkeypatch, 'bad-private-value')
    original_session = provider._get_session
    async def session():
        original = await original_session()
        class Session:
            def get(self, *args, **kwargs):
                response = original.get(*args, **kwargs)
                response.payload['output'] *= 600
                return response
        return Session()
    provider._get_session = session
    messages = []
    monkeypatch.setattr(kis_market_data.logger, 'warning', messages.append)
    capture = SourceCapture()
    rows = asyncio.run(capture.call('kis_foreign_buying', provider.fetch_foreign_institution,
                                   market='0001', investor='1'))
    source = next(row for row in capture.wrap(rows).selection_sources['runs']
                  if row['source_id'] == 'kis_foreign_buying')
    assert len(rows) == 600
    assert source['failure_count'] == 1 and source['failure_reason'] == 'schema'
    assert len(messages) == 1 and 'rows=512' in messages[0]
    assert 'bad-private-value' not in messages[0]
    asyncio.run(provider.fetch_foreign_institution(market='0001', investor='1'))
    assert len(messages) == 1 and len(calls) == 1


@pytest.mark.parametrize('positive,change,expected,reason', [
    (False, 5, 30, 'SPDI↓ 수급부재급등(가격+5.0%)'),
    (True, 2, 62, 'SPDI↑ 수급선행(가격+2.0%)'),
    (True, 8, 42, 'SPDI중립 수급후행(가격+8.0%)'),
])
def test_malformed_earlier_candidate_does_not_skip_later_spdi_adjustment(
        monkeypatch, positive, change, expected, reason):
    import asyncio
    from src.analytics.selection_supply_policy import record_evidence, supply_proof, supply_scan
    from src.signals.screener import kr_screener
    malformed = stock('000001', score=50, change_pct='malformed-private-value')
    valid = stock('000002', score=50, change_pct=change, has_foreign_buying=positive)
    candidates = {s.symbol: s for s in (malformed, valid)}
    warnings = []
    monkeypatch.setattr(kr_screener.logger, 'warning', warnings.append)
    scanner = object.__new__(StockScreener)

    @supply_scan
    async def evaluate():
        for investor in ('1', '2'):
            record_evidence(investor, {symbol: supply_proof('0') for symbol in candidates})
        await scanner._apply_spdi_filter(candidates)

    asyncio.run(evaluate())
    assert valid.score == expected
    assert valid.reasons == ['원천', reason]  # exactly one adjustment
    assert malformed.score == 50
    assert len(warnings) == 1
    assert 'malformed-private-value' not in warnings[0]
