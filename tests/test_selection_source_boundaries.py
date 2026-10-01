"""실제 원천 반환 분기에서 관측 on/off 결과·호출수와 원장 연결을 비교한다."""
import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from src.signals.screener import kr_screener as m
from src.analytics.selection_source_status import SourceCapture
from src.analytics.entry_observation import capture_scan
from src.analytics.selection_basis import build_selection_report
from test_selection_source_basis import screener, stock, run
from test_selection_source_capture import buffer
from test_selection_source_instrumentation import _Response


def direct(monkeypatch, payload, status=200):
    s = object.__new__(m.StockScreener)
    s._cache, s._cache_time, s._cache_ttl = {}, {}, 300
    s.min_volume_ratio, s.max_change_pct, s.min_trading_value = 2, 15, 100000000
    s._token_manager = SimpleNamespace(base_url='http://synthetic')
    calls = []
    class Session:
        def get(self, *args, **kw):
            calls.append('get')
            return _Response(status, payload)
    async def session(): return Session()
    async def headers(*a): return {}
    async def acquire(): pass
    s._get_session, s._get_headers = session, headers
    monkeypatch.setattr(m.kis_rate_limit, 'acquire', acquire)
    return s, calls


ITEM = {'mksc_shrn_iscd':'000001','hts_kor_isnm':'합성','stck_prpr':'10000',
        'prdy_ctrt':'2','acml_vol':'10000','vol_inrt':'200','prdy_vol':'5000'}


@pytest.mark.parametrize('method,source', [('screen_volume_surge','kis_volume_surge'), ('screen_new_highs','kis_new_highs')])
@pytest.mark.parametrize('case,outcome', [('empty','completed_empty'),('filtered','completed_empty'),
    ('success','completed_nonempty'),('http','failed'),('api','failed'),('missing','failed'),('partial','partial')])
def test_real_direct_kis_branches_preserve_results_and_one_request(monkeypatch, method, source, case, outcome):
    payload, status = {'rt_cd':'0','output':[ITEM]}, 200
    if case == 'empty': payload['output'] = []
    elif case == 'filtered': payload['output'] = [{**ITEM,'stck_prpr':'0'}]
    elif case == 'http': status = 500
    elif case == 'api': payload['rt_cd'] = '1'
    elif case == 'missing': payload.pop('output')
    elif case == 'partial': payload['output'] = [ITEM, {**ITEM,'stck_prpr':'invalid'}]
    plain, calls = direct(monkeypatch, payload, status)
    expected = asyncio.run(getattr(plain, method)())
    observed, traced_calls = direct(monkeypatch, payload, status); c = SourceCapture()
    actual = asyncio.run(c.call(source, getattr(observed, method)))
    fields = lambda rows:[(r.symbol,r.price,r.score,r.reasons,r.volume) for r in rows]
    assert fields(actual) == fields(expected)
    assert calls == traced_calls == ['get']
    row = next(r for r in c.wrap(actual).selection_sources['runs'] if r['source_id']==source)
    assert row['outcome'] == outcome
    if case in ('empty','filtered'):
        assert row['raw_count'] == (0 if case=='empty' else 1)
    if case == 'success':
        next_capture = SourceCapture()
        cached = asyncio.run(next_capture.call(source, getattr(observed, method)))
        row = next(r for r in next_capture.wrap(cached).selection_sources['runs'] if r['source_id']==source)
        assert row['outcome'] == 'cached_nonempty' and row['max_cache_age_seconds'] is not None
        assert traced_calls == ['get']


@pytest.mark.parametrize('response,outcome,count', [({'stocks':[]},'completed_empty',0),
    ({'error':'synthetic'},'failed',0), ({},'failed',0),
    ({'stocks':[{'name':'합성','symbol':'000001'},None]},'partial',1)])
def test_real_llm_payload_empty_error_missing_and_partial_preserve_return(monkeypatch, response, outcome, count):
    s = object.__new__(m.StockScreener); calls=[]
    async def hints(): return ''
    async def complete(*a, **kw): calls.append(1); return response
    s._get_stock_hints_for_llm = hints
    c = SourceCapture()
    result = asyncio.run(c.call('llm_news',s.extract_stocks_from_news,['합성 제목'],SimpleNamespace(complete_json=complete)))
    row = next(r for r in c.wrap(result).selection_sources['runs'] if r['source_id']=='llm_news')
    assert len(result)==count and calls==[1] and row['outcome']==outcome


def test_complete_screen_to_scan_report_including_old_cache_fallback(monkeypatch):
    sources={'screen_volume_surge':[stock()]}; s=screener(monkeypatch,sources)
    first=run(s,capture_selection=True,capture_selection_sources=True)
    b=buffer(); capture_scan(b,first,'regular'); report=build_selection_report(b.export())
    assert report['counts']['accounted']==1
    assert report['source_scans'][0]['snapshot']['fallback_used'] is False
    sources.clear()
    second=run(s,capture_selection=True,capture_selection_sources=True)
    b=buffer(); capture_scan(b,second,'regular'); report=build_selection_report(b.export())
    assert report['counts']['accounted']==report['counts']['cache_fallback']==1
    assert report['source_scans'][0]['snapshot']['fallback_used'] is True
    assert first[0].selection_basis['basis_id']==second[0].selection_basis['basis_id']
    volume=next(r for r in report['source_scans'][0]['snapshot']['runs'] if r['source_id']=='kis_volume_surge')
    assert volume['outcome']=='unknown_empty' and volume['returned_count']==0


@pytest.mark.parametrize('failure', ['marker','wrap','init'])
def test_observation_failures_do_not_remove_real_source_results(monkeypatch,failure):
    s, calls=direct(monkeypatch,{'rt_cd':'0','output':[ITEM]})
    # Preserve the real volume method while replacing unrelated external sources.
    real_volume=s.screen_volume_surge
    wrapped=screener(monkeypatch,{}); wrapped.screen_volume_surge=real_volume
    def broken(*a,**kw): raise RuntimeError('observer error')
    if failure=='marker': monkeypatch.setattr(m,'mark_source',broken)
    elif failure=='wrap': monkeypatch.setattr(m.SourceCapture,'wrap',broken)
    else: monkeypatch.setattr(m,'SourceCapture',broken)
    result=run(wrapped,capture_selection=True,capture_selection_sources=True)
    assert len(result)==1 and result[0].symbol=='000001' and calls==['get']
    assert result[0].selection_basis['source_terms'][0]['source_id']=='kis_volume_surge'
