"""LLM 이후 진입 게이트 재생과 장 상태 분류 — 합성 자료만, 네트워크·주문 없음."""
import copy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analytics import model_comparison_admission as adm
from src.analytics import model_comparison_sizing as sizing
from src.analytics.kis_market_state import _TR_SPECS
from src.core.engine import RiskManager
from src.core.event import SignalEvent
from src.core.types import OrderSide, RiskConfig, Signal, SignalStrength, StrategyType
from src.utils.stop_policy import StopDecision


@pytest.fixture
def case(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for module, function in [('calendar_seasonality', 'calendar_multiplier'),
                             ('volatility_targeting', 'vol_targeting_multiplier'),
                             ('team_conviction', 'team_conviction_multiplier')]:
        monkeypatch.setattr(f'src.utils.{module}.{function}', lambda *a: (1.0, 'fixture'))

    def build(cash='10000000', strategy_current='0', pnl='0'):
        rm = object.__new__(RiskManager)
        rm.config = RiskConfig(sizing_mode='nominal', max_position_pct=28,
                              min_position_value=200000, strategy_allocation={'sepa_trend': 40})
        rm.engine = SimpleNamespace(portfolio=SimpleNamespace(
            total_equity=Decimal('10000000'), effective_daily_pnl=Decimal(pnl),
            get_strategy_allocation=lambda s: Decimal(strategy_current)),
            get_available_cash=lambda: Decimal(cash))
        rm._reserved_by_order = {}
        rm._pending_strategy = {}
        rm._LLM_REJECT_SIZE_MULT = 0.5
        rm._resolve_entry_stop = lambda s: StopDecision(Decimal('5'), 'strategy', False)
        event = SignalEvent.from_signal(Signal(symbol='TEST', side=OrderSide.BUY,
            strategy=StrategyType.SEPA_TREND, strength=SignalStrength.NORMAL,
            price=Decimal('10000'), metadata={'position_multiplier': 0.8}))
        snapshot = sizing.capture_policy_sizing(rm, event)
        captured = datetime.fromisoformat(snapshot['captured_at'])
        envelope = {'decision_id': 'dec-1', 'neutral_sizing': snapshot,
                    'input_available_at': (captured + timedelta(milliseconds=5)).isoformat()}
        return envelope
    return build


def test_good_snapshot_passes_replayable_gates_but_external_state_keeps_unknown(case):
    envelope = case()
    replay = sizing.replay_policy_sizing(envelope['neutral_sizing'], 'approve')
    result = adm.replay_admission(envelope, 'approve', replay)
    gates = result['gates']
    for name in adm.REPLAYABLE_GATES:
        assert gates[name]['status'] == 'passed', name
        assert gates[name]['replayable'] is True
        assert gates[name]['as_of'] == envelope['neutral_sizing']['captured_at']
    for name in adm.EXTERNAL_GATES:
        assert gates[name] == {'status': 'unknown', 'as_of': None,
                               'reason': 'external_state_not_replayable', 'replayable': False}
    # 외부 상태가 unknown 인 한 통과를 만들지 않는다.
    assert result['admissible'] is None
    assert result['quantity'] == replay['quantity'] == 200
    # 경제성 계산이 행의 스냅샷·기준시각과 대조할 수 있도록 둘 다 싣는다.
    assert result['snapshot_id'] == envelope['neutral_sizing']['snapshot_id']
    assert result['anchor_at'] == envelope['input_available_at']


def test_zero_quantity_from_exhausted_strategy_budget_fails_with_engine_reason(case):
    envelope = case(strategy_current='4000000')     # 40% cap 이 이미 소진
    replay = sizing.replay_policy_sizing(envelope['neutral_sizing'], 'approve')
    assert replay['quantity'] == 0 and replay['trace']['stop'] == 'strategy_budget_exhausted'
    result = adm.replay_admission(envelope, 'approve', replay)
    gates = result['gates']
    assert gates['sizing_quantity'] == {'status': 'failed', 'as_of': envelope['neutral_sizing']['captured_at'],
                                        'reason': 'size_zero:strategy_budget_exhausted', 'replayable': True}
    assert gates['sizing_strategy_budget']['status'] == 'failed'
    assert gates['strategy_budget_precheck']['status'] == 'failed'
    assert gates['sizing_min_position_value']['status'] == 'unknown'   # 평가 전 단계에서 멈춤
    assert result['admissible'] is False


def test_no_cash_and_daily_hard_cap_fail(case):
    envelope = case(cash='0')
    replay = sizing.replay_policy_sizing(envelope['neutral_sizing'], 'approve')
    result = adm.replay_admission(envelope, 'approve', replay)
    assert result['gates']['cash_precheck']['status'] == 'failed'
    assert result['gates']['sizing_available_cash']['reason'] == 'size_zero:no_available_cash'
    assert result['admissible'] is False
    envelope = case(pnl='-1300000')                  # -13% <= -5*2.5
    replay = sizing.replay_policy_sizing(envelope['neutral_sizing'], 'approve')
    result = adm.replay_admission(envelope, 'approve', replay)
    assert result['gates']['engine_daily_hard_cap']['status'] == 'failed'
    assert result['admissible'] is False


def test_later_snapshot_cannot_leak_into_earlier_arm(case):
    early = case()
    later = case(cash='50000000')                    # 나중 장부(더 많은 현금)
    replay = sizing.replay_policy_sizing(later['neutral_sizing'], 'approve')
    leaked = dict(early, neutral_sizing=later['neutral_sizing'])
    leaked['input_available_at'] = (datetime.fromisoformat(later['neutral_sizing']['captured_at'])
                                    - timedelta(seconds=1)).isoformat()
    result = adm.replay_admission(leaked, 'approve', replay)
    assert result['reasons'] == ['snapshot_after_anchor']
    assert all(g['status'] == 'unknown' for g in result['gates'].values())
    assert result['admissible'] is None
    # 다른 스냅샷의 재생 결과를 섞어도 unknown
    other = sizing.replay_policy_sizing(later['neutral_sizing'], 'approve')
    assert adm.replay_admission(early, 'approve', other)['reasons'] == ['replay_snapshot_or_verdict_mismatch']
    assert adm.replay_admission(early, 'reject', sizing.replay_policy_sizing(early['neutral_sizing'], 'approve')
                                )['admissible'] is None


@pytest.mark.parametrize('mutate,reason', [
    (lambda e: e['neutral_sizing']['state'].update(available_cash='1'), 'snapshot_fingerprint_mismatch'),
    (lambda e: e.pop('input_available_at'), 'anchor_missing'),
    (lambda e: e.update(neutral_sizing=None), 'snapshot_missing'),
])
def test_tampered_or_missing_evidence_is_unknown(case, mutate, reason):
    envelope = case()
    replay = sizing.replay_policy_sizing(envelope['neutral_sizing'], 'approve')
    envelope = copy.deepcopy(envelope)
    mutate(envelope)
    result = adm.replay_admission(envelope, 'approve', replay)
    assert result['reasons'] == [reason] and result['admissible'] is None


# ── 장 상태 ─────────────────────────────────────────────────────────────

NOW = '2026-10-15T01:00:10+00:00'


def cnt(**over):
    rec = {'schema_version': 'kis-market-state-raw-v1', 'kind': 'market_state', 'tr_id': 'H0STCNT0',
           'market_state_schema': 'kis-public-guide-20261012',
           'market_state_source_sha256': _TR_SPECS['H0STCNT0']['source_sha256'],
           'market_state_status': 'raw_only', 'symbol': '005930', 'connection_id': 'c1', 'generation': 3,
           'received_at': '2026-10-15T01:00:08+00:00', 'local_frame_index': 10, 'record_index': 1,
           'new_mkop_cls_code': '20', 'trht_yn': 'N', 'hour_cls_code': '0', 'market_cls_code': '2'}
    rec.update(over)
    return rec


def mko(**over):
    rec = cnt(tr_id='H0STMKO0', market_state_source_sha256=_TR_SPECS['H0STMKO0']['source_sha256'],
              received_at='2026-10-15T00:00:01+00:00', local_frame_index=2,
              mkop_cls_code='112', vi_cls_code='N', iscd_stat_cls_code='00', trht_yn='N')
    for key in ('new_mkop_cls_code', 'hour_cls_code', 'market_cls_code'):
        rec.pop(key)
    rec.update(over)
    return rec


def classify(records):
    return adm.classify_market_state(records, now=NOW, max_age_seconds=5,
                                     expected_connection_id='c1', expected_generation=3)


def test_good_same_generation_evidence_is_normal_continuous():
    result = classify([cnt(), mko()])
    assert result['verdict'] == 'normal_continuous'
    assert {r['tr_id'] for r in result['source_refs']} == {'H0STCNT0', 'H0STMKO0'}
    assert result['evaluated_at'] == NOW


@pytest.mark.parametrize('records,verdict,basis', [
    ([], 'unknown', 'missing_events'),
    ([cnt()], 'unknown', 'missing_events:H0STMKO0'),             # 사건식 장운영 알림 침묵
    ([mko()], 'unknown', 'missing_events:H0STCNT0'),
    ([cnt(hour_cls_code=''), mko()], 'unknown', 'empty_value:H0STCNT0'),
    ([cnt(received_at='2026-10-15T01:00:01+00:00'), mko()], 'unknown', 'stale:H0STCNT0'),
    ([cnt(generation=2), mko()], 'unknown', 'other_connection_or_generation:H0STCNT0'),  # 재연결 전 세대
    ([cnt(), mko(generation=2)], 'unknown', 'latest_record_unqualified:H0STMKO0'),
    ([cnt(connection_id='c0'), mko()], 'unknown', 'other_connection_or_generation:H0STCNT0'),
    ([cnt(tr_id='H0STASP0'), mko()], 'unknown', 'unqualified_record:H0STASP0'),        # 다른 채널
    ([cnt(market_state_status='invalid_values'), mko()], 'unknown', 'unqualified_record:H0STCNT0'),
    ([cnt(hour_cls_code='C'), mko()], 'not_normal', 'not_normal:H0STCNT0.hour_cls_code=C'),   # VI
    ([cnt(), mko(vi_cls_code='Y')], 'unknown', 'channel_conflict'),
    ([cnt(trht_yn='Y'), mko()], 'not_normal', 'not_normal:H0STCNT0.trht_yn=Y'),        # 정지
    ([cnt(), mko(iscd_stat_cls_code='58')], 'unknown', 'channel_conflict'),
    ([cnt(new_mkop_cls_code='20', market_cls_code='5'), mko()], 'not_normal', 'not_normal:H0STCNT0.market_cls_code=5'),
    ([cnt(new_mkop_cls_code='29'), mko()], 'unknown', 'unknown_code:H0STCNT0.new_mkop_cls_code=29'),
    ([cnt(), mko(mkop_cls_code='F17')], 'unknown', 'unknown_code:H0STMKO0=F17/vi=N/iscd=00'),
    ([cnt(), mko(iscd_stat_cls_code='59')], 'unknown', 'unknown_code:H0STMKO0=112/vi=N/iscd=59'),
    ([cnt(), mko(), cnt(symbol='000660', received_at='2026-10-15T01:00:09+00:00')], 'unknown', 'mixed_symbols'),
    ([cnt(hour_cls_code='C'), mko(mkop_cls_code='164')], 'not_normal', 'not_normal:H0STMKO0.mkop_cls_code=164'),
])
def test_incomplete_or_contrary_evidence_never_becomes_normal(records, verdict, basis):
    result = classify(records)
    assert result['verdict'] == verdict
    assert basis in result['basis']


def test_records_after_now_are_ignored_and_latest_wins():
    # now 이후의 정상 기록으로 이전의 VI 상태를 덮지 않는다.
    future = cnt(received_at='2026-10-15T01:00:30+00:00')
    assert classify([cnt(hour_cls_code='C'), future, mko()])['verdict'] == 'not_normal'
    # 같은 세대에서 VI 뒤의 장중 체결이 최신이면 그 값을 쓴다.
    older = cnt(hour_cls_code='C', received_at='2026-10-15T01:00:07+00:00', local_frame_index=9)
    assert classify([older, cnt(), mko()])['verdict'] == 'normal_continuous'


def test_invalid_parameters_are_unknown():
    assert adm.classify_market_state([cnt(), mko()], now='bad', max_age_seconds=5, expected_connection_id='c1',
                                     expected_generation=3)['verdict'] == 'unknown'
    assert adm.classify_market_state([cnt(), mko()], now=NOW, max_age_seconds=True, expected_connection_id='c1',
                                     expected_generation=3)['verdict'] == 'unknown'


LATER = {'received_at': '2026-10-15T01:00:09+00:00', 'local_frame_index': 11}


@pytest.mark.parametrize('later', [
    cnt(market_state_status='invalid_values', **LATER),
    cnt(market_state_status='unsupported_layout', **LATER),
    cnt(market_state_source_sha256='0' * 64, **LATER),
    cnt(generation=4, **LATER),             # 세대 전환 뒤 상태 불명
    cnt(connection_id='c2', **LATER),
])
def test_latest_unqualified_record_is_not_replaced_by_earlier_normal(later):
    result = classify([cnt(), later, mko()])
    assert result['verdict'] == 'unknown'
    assert 'latest_record_unqualified:H0STCNT0' in result['basis']


def test_latest_unqualified_market_operation_record_is_unknown():
    later_mko = mko(market_state_status='invalid_values', received_at='2026-10-15T00:30:00+00:00')
    result = classify([cnt(), mko(), later_mko])
    assert result['verdict'] == 'unknown'
    assert 'latest_record_unqualified:H0STMKO0' in result['basis']


def test_unreadable_time_on_channel_record_makes_channel_unknown():
    result = classify([cnt(), cnt(received_at='not-a-time'), mko()])
    assert result['verdict'] == 'unknown'
    assert 'invalid_received_at:H0STCNT0' in result['basis']


def test_earlier_unqualified_record_does_not_block_later_normal():
    earlier = cnt(market_state_status='invalid_values', received_at='2026-10-15T01:00:07+00:00', local_frame_index=9)
    other_gen = cnt(generation=2, received_at='2026-10-15T01:00:06+00:00', local_frame_index=8)
    assert classify([earlier, other_gen, cnt(), mko()])['verdict'] == 'normal_continuous'
    # now 이후의 무효 기록은 판정에서 제외된다
    future_bad = cnt(market_state_status='invalid_values', received_at='2026-10-15T01:00:30+00:00')
    assert classify([cnt(), future_bad, mko()])['verdict'] == 'normal_continuous'
