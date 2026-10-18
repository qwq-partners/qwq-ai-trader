"""Bounded, opt-in arithmetic observations; never an input to production scoring."""
from contextvars import ContextVar
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import re

ORDER = ('dual_supply_bonus', 'naver_merge', 'theme_or_llm_news_bonus', 'valuation',
         'momentum', 'rs_ranking', 'volume_ratio_enrichment', 'sector_diversity',
         'sector_rotation', 'volatility', 'spdi', 'institution_sell_blacklist',
         'dart_catalyst', 'supply_history_record', 'supply_accumulation',
         'theme_leader_follower', 'catalyst_lifecycle', 'retail_only', 'overnight_regime',
         'final_etf_filter', 'tradeable_universe_filter', 'minimum_price_filter',
         'occurrence_bonus', 'normalization')
EXTRA_FIELDS = frozenset(('supply_evidence', 'modifier_stages', 'modifier_sum',
                         'modifier_trace_complete', 'same_input_shadow'))
STAGE_FIELDS = frozenset(('ordinal', 'stage_id', 'before', 'after', 'merge_delta', 'delta', 'status'))
SUPPLY_FIELDS = frozenset(('state', 'value', 'unit', 'semantics', 'market_scope', 'captured_at',
                          'source_as_of', 'origin', 'legacy_flag'))
SHADOW_FIELDS = frozenset(('modifier_id', 'status', 'actual_before', 'actual_after', 'change_pct',
                         'native_evaluated', 'shadow_after', 'shadow_delta_vs_actual',
                         'final_score_delta', 'gate_delta', 'economic_effect'))
_ACTIVE = ContextVar('selection_modifiers', default=None)
_SOURCE = ContextVar('selection_supply_source', default=None)


def observe(method, *args):
    """All hooks fail closed for evidence, without touching business objects."""
    capture = _ACTIVE.get()
    if capture is None:
        return
    try:
        getattr(capture, method)(*args)
    except Exception:
        capture.failed = True


def capture_scope(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        capture = None
        if kwargs.get('capture_selection_modifiers') is True and kwargs.get('capture_selection') is True:
            try:
                capture = ModifierCapture()
            except Exception:
                pass
        token = _ACTIVE.set(capture)
        try:
            return await func(*args, **kwargs)
        finally:
            _ACTIVE.reset(token)
    return wrapped


@contextmanager
def source_scope(source_id):
    token = _SOURCE.set(source_id)
    try:
        yield
    finally:
        _SOURCE.reset(token)


def mark_candidate_supply(investor, market, symbol, raw):
    """Only fresh FHPTJ04400000 rows with a matching active source qualify."""
    observe('raw', investor, market, symbol, raw, _SOURCE.get())


def current_extension(stock):
    capture = _ACTIVE.get()
    if capture is None or capture.failed:
        return None
    try:
        return capture.finish(stock)
    except Exception:
        capture.failed = True
        return None


class ModifierCapture:
    def __init__(self):
        self.failed = False
        self.rows, self.stages, self.merges, self.native = {}, [], {}, {}
        self.open = None

    def raw(self, investor, market, symbol, raw, source):
        if source != {'1':'kis_foreign_buying', '2':'kis_institutional_buying'}.get(investor):
            return
        if investor not in ('1','2') or market not in ('0001','0002') or not isinstance(symbol,str) or re.fullmatch(r'[0-9]{6}',symbol) is None or symbol == '000000':
            return
        if type(raw) is int:
            qty = raw
        elif type(raw) is str and len(raw) <= 14 and re.fullmatch(r'[+-]?[0-9]+', raw):
            qty = int(raw)
        else:
            return
        if abs(qty) > 10**12:
            return
        key = (investor,symbol)
        if key not in self.rows and len(self.rows) >= 512:
            self.failed = True
            return
        row = (qty,market,datetime.now(timezone.utc).isoformat())
        if key in self.rows and (self.rows[key] is None or self.rows[key][:2] != row[:2]):
            self.rows[key] = None
        else:
            self.rows[key] = row

    def merge(self, symbol, amount):
        from .selection_basis import number
        if symbol not in self.merges and len(self.merges) >= 500:
            raise ValueError('modifier candidate cap')
        self.merges[symbol] = number(self.merges.get(symbol,0) + number(amount))

    def begin(self, stage, stocks):
        from .selection_basis import number
        if self.open is not None or stage != ORDER[len(self.stages)] or len(stocks)>500:
            raise ValueError('modifier stage order/cap')
        values = stocks.values() if isinstance(stocks,dict) else stocks
        self.open = (stage,{s.symbol:number(s.score) for s in values},dict(self.merges))

    def end(self, stage, stocks):
        from .selection_basis import number
        old, before, merges = self.open
        if old != stage or len(stocks)>500:
            raise ValueError('modifier stage mismatch/cap')
        values = stocks.values() if isinstance(stocks,dict) else stocks
        after = {s.symbol:number(s.score) for s in values}
        self.stages.append((stage,before,after,{s:number(self.merges.get(s,0)-merges.get(s,0)) for s in after}))
        self.open = None

    def spdi(self, stock):
        from .selection_basis import number
        if self.open is None or self.open[0] != 'spdi':
            return
        self.native[stock.symbol] = (number(stock.change_pct), stock.has_foreign_buying, stock.has_inst_buying)

    def finish(self, stock):
        if len(self.stages) != len(ORDER) or self.open is not None:
            raise ValueError('incomplete modifier stages')
        evidence = {}
        for investor,name,flag in [('1','foreign',stock.has_foreign_buying),('2','institution',stock.has_inst_buying)]:
            raw = self.rows.get((investor,stock.symbol))
            evidence[name] = dict(state=('positive' if raw[0]>0 else 'observed_nonpositive') if raw else 'unobserved',
                value=raw[0] if raw else None,unit='shares',semantics='FHPTJ04400000.ntby_qty',
                market_scope=raw[1] if raw else None,captured_at=raw[2] if raw else None,source_as_of=None,
                origin='current_raw_row' if raw else 'conflict' if (investor,stock.symbol) in self.rows else 'not_seen',legacy_flag=flag)
        stages=[]
        for i,(stage,before,after,merges) in enumerate(self.stages,1):
            b,a=before.get(stock.symbol,0),after.get(stock.symbol,0)
            merged=merges.get(stock.symbol,0)
            delta=a-b-merged
            status='changed_observed' if delta else 'not_evaluated'
            if stage=='spdi' and stock.symbol in self.native:
                status='completed_changed' if delta else 'completed_unchanged'
            stages.append(dict(ordinal=i,stage_id=stage,before=b,after=a,merge_delta=merged,delta=delta,status=status))
        total=sum(s['delta'] for s in stages[:-2])
        native=self.native.get(stock.symbol)
        spdi=stages[10]
        shadow=dict(modifier_id='spdi_supply_absence',status='inputs_incomplete',actual_before=None,
            actual_after=None,change_pct=None,native_evaluated=False,shadow_after=None,shadow_delta_vs_actual=None,
            final_score_delta=None,gate_delta=None,economic_effect=None)
        if native and all(v["origin"] != "conflict" for v in evidence.values()):
            change,foreign,inst=native
            expected=_spdi(spdi['before'],change,foreign,inst)
            if expected==spdi['after'] and (foreign,inst)==(stock.has_foreign_buying,stock.has_inst_buying):
                value=expected
                if not (foreign or inst) and change>=5 and any(v['state']=='unobserved' for v in evidence.values()):
                    value=spdi['before']
                shadow.update(status='comparable',actual_before=spdi['before'],actual_after=spdi['after'],
                    change_pct=change,native_evaluated=True,shadow_after=value,shadow_delta_vs_actual=value-expected)
        return dict(supply_evidence=evidence,modifier_stages=stages,modifier_sum=total,
                    modifier_trace_complete=all(v['origin']!='conflict' for v in evidence.values()),same_input_shadow=shadow)


def _spdi(before, change, foreign, inst):
    if (foreign or inst) and 0<change<=3:
        return before+(20 if foreign and inst else 12)
    if not (foreign or inst) and change>=5:
        return max(before-20,0)
    if (foreign or inst) and change>=8:
        return max(before-8,0)
    return before


def validate_extension(basis):
    from .selection_basis import number, _equal, _time
    supply=basis['supply_evidence']
    if type(supply) is not dict or set(supply)!={'foreign','institution'}:
        raise ValueError('supply investor fields')
    conflict=False
    for row in supply.values():
        if type(row) is not dict or set(row)!=SUPPLY_FIELDS or row['unit']!='shares' or row['semantics']!='FHPTJ04400000.ntby_qty' or row['source_as_of'] is not None or type(row['legacy_flag']) is not bool:
            raise ValueError('supply schema')
        if row['state']=='unobserved':
            if row['origin'] not in ('not_seen','conflict') or any(row[k] is not None for k in ('value','market_scope','captured_at')):
                raise ValueError('unobserved numeric claim')
            conflict |= row['origin']=='conflict'
        elif row['state'] in ('positive','observed_nonpositive'):
            if type(row['value']) is not int or number(row['value']) != row['value'] or row['origin']!='current_raw_row' or row['market_scope'] not in ('0001','0002') or _time(row['captured_at'])>_time(basis['produced_at']) or (row['value']>0)!=(row['state']=='positive'):
                raise ValueError('supply raw semantics')
        else:
            raise ValueError('supply state')
    stages=basis['modifier_stages']
    if type(stages) is not list or len(stages)!=len(ORDER):
        raise ValueError('modifier length')
    for i,row in enumerate(stages):
        if type(row) is not dict or set(row)!=STAGE_FIELDS or type(row['ordinal']) is not int or row['ordinal']!=i+1 or row['stage_id']!=ORDER[i] or row['status'] not in ('changed_observed','not_evaluated','completed_changed','completed_unchanged'):
            raise ValueError('modifier stage schema')
        if not _equal(row['delta'],number(row['after'])-number(row['before'])-number(row['merge_delta'])) or (i and not _equal(row['before'],stages[i-1]['after'])):
            raise ValueError('modifier delta/chain')
        if row['merge_delta'] and i not in (1,2):
            raise ValueError('unexpected interleaved merge')
        if (row['status'] in ('changed_observed','completed_changed')) != (row['delta']!=0):
            raise ValueError('modifier status/delta')
        if row['status'].startswith('completed') and row['stage_id']!='spdi':
            raise ValueError('unverified evaluation success')
    if (not _equal(stages[0]['before']+sum(s['merge_delta'] for s in stages),basis['merge_score_sum'])
            or not _equal(sum(s['delta'] for s in stages[:-2]),basis['modifier_sum'])
            or not _equal(basis['modifier_sum'],basis['post_merge_adjustment'])
            or not _equal(stages[-2]['delta'],basis['occurrence_bonus'])
            or not _equal(stages[-1]['before'],basis['pre_normalization_score'])
            or not _equal(stages[-1]['after'],basis['final_score'])
            or type(basis['modifier_trace_complete']) is not bool or basis['modifier_trace_complete']==conflict):
        raise ValueError('modifier reconciliation')
    shadow=basis['same_input_shadow']
    if type(shadow) is not dict or set(shadow)!=SHADOW_FIELDS or shadow['modifier_id']!='spdi_supply_absence' or any(shadow[k] is not None for k in ('final_score_delta','gate_delta','economic_effect')):
        raise ValueError('shadow schema/economic claim')
    nums=('actual_before','actual_after','change_pct','shadow_after','shadow_delta_vs_actual')
    if shadow['status']=='inputs_incomplete':
        if shadow['native_evaluated'] is not False or any(shadow[k] is not None for k in nums):
            raise ValueError('incomplete shadow claim')
    elif shadow['status']=='comparable':
        spdi=stages[10]
        foreign,inst=supply['foreign']['legacy_flag'],supply['institution']['legacy_flag']
        change=number(shadow['change_pct']);before=spdi['before']
        actual=_spdi(before,change,foreign,inst)
        expected=before if not(foreign or inst) and change>=5 and any(r['state']=='unobserved' for r in supply.values()) else actual
        if (shadow['native_evaluated'] is not True or not spdi['status'].startswith('completed') or not basis['modifier_trace_complete']
                or not _equal(shadow['actual_before'],before) or not _equal(shadow['actual_after'],spdi['after'])
                or not _equal(actual,spdi['after']) or not _equal(shadow['shadow_after'],expected)
                or not _equal(shadow['shadow_delta_vs_actual'],expected-actual)):
            raise ValueError('shadow native SPDI reconciliation')
    else:
        raise ValueError('shadow status')
