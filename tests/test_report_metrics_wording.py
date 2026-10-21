from src.analytics.counterfactual_tracker import CounterfactualTracker
from src.analytics.gate_performance import GatePerformanceAnalyzer


def test_unfilled_price_outcomes_are_not_trading_accuracy():
    tracker = object.__new__(CounterfactualTracker)
    tracker._fill_evidence_check = lambda *args: False
    tracker._state = {'a': {'source': 'team_buy_unfilled', 'r5': -5, 'r20': None, 'x5': -2},
                      'b': {'source': 'gate', 'r5': -4, 'r20': None, 'x5': -1}}
    text = tracker.summary()
    assert '적중' not in text and '팀 판단이 틀' not in text
    assert '체결·비용 미반영' in text and '하락' in text


def test_gate_forward_returns_are_observations_not_realized_opportunity_loss():
    text = GatePerformanceAnalyzer.format_report({
        'lookback_days': 30, 'horizon_days': 5, 'total_analyzed': 10, 'verdicts': ['게이트가 수익을 버리고 있음. 완화 검토'],
        'gates': {'G2_cross': {'samples': 10, 'avg_return': 1, 'opportunity_loss_pct': 40, 'avoided_pct': 20}}})
    assert '회피성공' not in text and '기회손실' not in text
    assert '수익을 버리고' not in text
    assert '체결·비용 미반영' in text and '5영업일' in text
