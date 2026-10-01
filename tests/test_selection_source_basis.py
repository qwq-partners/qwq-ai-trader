"""합성 원천으로 선정 점수·캐시 소유권·관측 근거를 검증한다."""
import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.signals.screener import kr_screener as m


class Noon(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 1, 12, 0, tzinfo=tz)


def screener(monkeypatch, sources):
    monkeypatch.setattr(m, 'datetime', Noon)
    s = object.__new__(m.StockScreener)
    s._cache, s._cache_time, s._cache_ttl = {}, {}, 300
    s._stock_master, s._code_to_name = None, {}
    async def empty(*a, **kw): return []
    async def noop(*a, **kw): pass
    for name in ('screen_volume_surge', 'screen_institutional_buying', 'screen_new_highs',
                 'screen_fluctuation_rank', 'screen_foreign_buying', 'screen_premarket_gap',
                 'naver_volume_rank', 'naver_rise_rank'):
        async def source(*a, _name=name, **kw): return sources.get(_name, [])
        setattr(s, name, source)
    for name in ('_apply_valuation_bonus', '_apply_momentum_filter', '_apply_rs_ranking_bonus',
                 '_apply_volume_ratio', '_apply_sector_diversity', '_apply_sector_rotation_bonus',
                 '_apply_volatility_filter', '_apply_spdi_filter', '_apply_inst_sell_blacklist',
                 '_apply_dart_catalyst'):
        setattr(s, name, noop)
    s._record_supply_demand = lambda *_: None
    s._apply_supply_accumulation_bonus = lambda *_: None
    return s


def stock(symbol='000001', score=100, **kw):
    return m.ScreenedStock(symbol, name='합성 종목', price=10000, score=score, reasons=['원천'], **kw)


def run(s, **kw):
    return asyncio.run(s.screen_all(**kw))


def test_merge_preserves_source_score_reasons_flags_and_repeated_input(monkeypatch):
    a, b = stock(), stock(score=60, has_inst_buying=True)
    sources = {'screen_volume_surge': [a], 'screen_institutional_buying': [b]}
    s = screener(monkeypatch, sources); before = (asdict(a), asdict(b))
    first = run(s); second = run(s)
    assert first[0] is not a and first[0] is not b
    assert first[0].score == second[0].score == 78
    assert first[0].has_inst_buying is True
    assert (asdict(a), asdict(b)) == before


def test_modifier_fields_do_not_flow_back_to_source_cache(monkeypatch):
    a = stock(); s = screener(monkeypatch, {'screen_volume_surge': [a]}); before = asdict(a)
    async def adjust(stocks):
        x = stocks[a.symbol]; x.score += 7; x.rsi = 66; x.atr_pct = 3; x.per = 8
        x.reasons.append('합성 보정')
    s._apply_valuation_bonus = adjust
    result = run(s)
    assert result[0].score == 57 and result[0].rsi == 66
    assert asdict(a) == before


def test_final_cache_and_each_fallback_have_separate_mutable_ownership(monkeypatch):
    sources = {'screen_volume_surge': [stock()]}; s = screener(monkeypatch, sources)
    result = run(s); expected = deepcopy([asdict(x) for x in result])
    result[0].score = -99; result[0].reasons.append('호출자 수정'); result.clear()
    sources.clear()
    fallback = run(s)
    assert [asdict(x) for x in fallback] == expected
    fallback[0].score = -42; fallback[0].reasons.append('폴백 수정'); fallback.clear()
    assert [asdict(x) for x in run(s)] == expected


def test_same_call_naver_derived_source_uses_original_rise_score(monkeypatch):
    rise = stock(score=90, change_pct=10, has_inst_buying=True)
    s = screener(monkeypatch, {'naver_rise_rank': [rise]})
    out = run(s, use_naver=True)
    assert rise.score == 90
    assert s._cache['naver_new_high'][0].score == 100
    assert out[0].score == 77  # 90*.3 + 100*.4 + 출현2회 보너스10


def test_opt_in_basis_reconciles_merge_adjustment_bonus_and_normalization(monkeypatch):
    a, b = stock(score=160), stock(score=100, has_inst_buying=True)
    s = screener(monkeypatch, {'screen_volume_surge': [a], 'screen_institutional_buying': [b]})
    async def adjust(stocks): stocks[a.symbol].score += 7
    s._apply_valuation_bonus = adjust
    result = run(s, capture_selection=True); basis = result[0].selection_basis
    assert [x['source_id'] for x in basis['source_terms']] == ['kis_volume_surge', 'kis_institutional_buying']
    assert [x['score_at_merge'] for x in basis['source_terms']] == [160, 100]
    assert [x['weighted_score'] for x in basis['source_terms']] == [80, 30]
    assert basis['merge_score_sum'] == 110 and basis['post_merge_adjustment'] == 7
    assert basis['occurrence_count'] == 2 and basis['occurrence_bonus'] == 10
    assert basis['pre_normalization_score'] == 127 and basis['final_score'] == 100
    assert basis['normalization_mode'] == 'clamp' and basis['returned_rank'] == 1
    assert basis['cache_fallback'] is False and basis['source_status_known'] is False
    assert basis['source_as_of_known'] is False
    assert getattr(a, 'selection_basis', None) is None


def test_affine_basis_keeps_cohort_extrema_and_returned_order(monkeypatch):
    a, b = stock('000001', 240), stock('000002', 80)
    s = screener(monkeypatch, {'screen_volume_surge': [a, b]})
    out = run(s, capture_selection=True)
    assert [x.score for x in out] == [100, 30]
    assert [x.selection_basis['pre_normalization_score'] for x in out] == [120, 40]
    assert all(x.selection_basis['normalization_min'] == 40 and
               x.selection_basis['normalization_max'] == 120 and
               x.selection_basis['normalization_mode'] == 'affine_30_100' for x in out)


def test_default_has_no_selection_basis_and_capture_does_not_change_scores(monkeypatch):
    s = screener(monkeypatch, {'screen_volume_surge': [stock()]})
    plain = run(s); traced = run(s, capture_selection=True)
    assert getattr(plain[0], 'selection_basis', None) is None
    assert plain[0].score == traced[0].score
    assert plain[0].reasons == traced[0].reasons


def test_fallback_labels_old_basis_and_does_not_invent_missing_basis(monkeypatch):
    sources = {'screen_volume_surge': [stock()]}; s = screener(monkeypatch, sources)
    first = run(s, capture_selection=True); sources.clear()
    fallback = run(s, capture_selection=True)
    assert fallback[0].selection_basis['cache_fallback'] is True
    assert fallback[0].selection_basis['basis_id'] == first[0].selection_basis['basis_id']
    assert first[0].selection_basis['cache_fallback'] is False
    sources['screen_volume_surge'] = [stock()]
    run(s); sources.clear()
    assert getattr(run(s, capture_selection=True)[0], 'selection_basis', None) is None


@pytest.mark.parametrize('method', ['record', 'before_bonus', 'normalization', 'finish'])
def test_optional_basis_exception_cannot_change_screen_result(monkeypatch, method):
    s = screener(monkeypatch, {'screen_volume_surge': [stock()]})
    def broken(*args, **kwargs): raise RuntimeError('합성 관측 오류')
    monkeypatch.setattr(m.SelectionBasisBuilder, method, broken)
    out = run(s, capture_selection=True)
    assert out[0].score == 50
    assert out[0].selection_basis is None
