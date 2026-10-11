from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from src.analytics.entry_capital_limits import CapitalLimits
from src.analytics.h1_capital_replay import ReplayOpportunity, replay_sleeve


KST = ZoneInfo('Asia/Seoul')
MULTIPLIER = Decimal('1.015')


def at(hour, minute=0):
    return datetime(2026, 10, 1, hour, minute, tzinfo=KST)


def limits(cash='1000', strategy='1000', position='1000', risk='1000'):
    return CapitalLimits(Decimal(cash), None if strategy is None else Decimal(strategy),
                         Decimal(position), Decimal(risk), Decimal('10'), 'synthetic', 'synthetic')


def opportunity(key, *, date='2026-10-01', symbol='AAA', ready_at=None, entry_at=None,
                entry_sequence=2, order_sequence=1, gate_status='allow', quantity=1,
                entry_price='100', entry_cost='100', buy_fee='0', capital_limits=None,
                exit_at=None, exit_sequence=3, exit_proceeds='110', sell_fee='0',
                reference_notional='100', reservation='101.500', unknown_reason=None):
    ready_at = ready_at or at(9)
    entry_at = entry_at or at(9, 1)
    exit_at = exit_at if exit_at is not None else at(9, 2)
    return ReplayOpportunity(
        key=key, date=date, symbol=symbol, ready_at=ready_at, reservation=Decimal(reservation),
        reference_notional=Decimal(reference_notional), entry_at=entry_at,
        entry_sequence=entry_sequence, order_sequence=order_sequence, gate_status=gate_status,
        quantity=quantity, entry_price=Decimal(entry_price), entry_cost=Decimal(entry_cost),
        buy_fee=Decimal(buy_fee), limits=capital_limits or limits(), exit_at=exit_at,
        exit_sequence=exit_sequence, exit_proceeds=Decimal(exit_proceeds), sell_fee=Decimal(sell_fee),
        unknown_reason=unknown_reason,
    )


def replay(rows, *, dates=('2026-10-01',), unknown_dates=(), cash='300', capacity=None):
    return replay_sleeve(rows, scheduled_dates=list(dates), unknown_dates=set(unknown_dates),
                         initial_cash=None if cash is None else Decimal(cash),
                         initial_strategy_capacity=None if capacity is None else Decimal(capacity),
                         pending_reservation_multiplier=MULTIPLIER)


def test_release_after_exit_allows_next_overlapping_candidate_and_carries_profit_to_next_day():
    """Removing exit releases from the event stream must leave the second candidate blocked."""
    first = opportunity('one', entry_cost='200', exit_proceeds='240', reservation='203.000',
                        reference_notional='200', quantity=2, entry_price='100',
                        exit_at=at(9, 2), exit_sequence=5)
    blocked = opportunity('blocked', ready_at=at(9, 1), entry_at=at(9, 1), order_sequence=1,
                          entry_sequence=3, entry_cost='150', exit_proceeds='160',
                          reservation='152.250', reference_notional='150', quantity=1,
                          entry_price='150', exit_at=at(9, 3))
    next_day = opportunity('next', date='2026-10-02', ready_at=datetime(2026, 10, 2, 9, tzinfo=KST),
                           entry_at=datetime(2026, 10, 2, 9, 1, tzinfo=KST),
                           exit_at=datetime(2026, 10, 2, 9, 2, tzinfo=KST), entry_cost='320', buy_fee='20',
                           exit_proceeds='300', reservation='304.500', reference_notional='300',
                           quantity=2, entry_price='150')
    result = replay([first, blocked, next_day], dates=('2026-10-01', '2026-10-02'), cash='350')

    assert result['opportunities'][1]['a_status'] == 'capital_blocked'
    assert result['daily'][0]['a_net_pnl'] == '40'
    assert result['daily'][1]['a_net_pnl'] == '-20'
    assert Decimal(result['arms']['A']['ending_cash']) == Decimal('370')
    assert result['arms']['A']['capital_blocked_count'] == 1


def test_b_cash_skip_releases_only_at_first_quote_and_can_fund_later_opportunity():
    """Changing B to release at ready would incorrectly admit the second ready reservation."""
    skipped = opportunity('skip', gate_status='cash', entry_cost='200', exit_proceeds='100',
                          reservation='203.000', reference_notional='200', quantity=2)
    later = opportunity('later', ready_at=at(9, 1), entry_at=at(9, 2), order_sequence=2,
                        entry_sequence=4, entry_cost='200', exit_proceeds='230',
                        reservation='203.000', reference_notional='200', quantity=2,
                        exit_at=at(9, 3))
    result = replay([skipped, later], cash='250')

    rows = {row['key']: row for row in result['opportunities']}
    assert rows['skip']['b_status'] == 'cash_skip'
    assert rows['later']['b_status'] == 'capital_blocked'
    assert result['arms']['B']['net_pnl'] == '0'
    assert result['arms']['B']['price_skip_count'] == 1


def test_ready_reservations_prevent_two_orders_from_double_spending_before_quotes():
    """Dropping ready reservations would let both entries use the same 300 KRW seed."""
    one = opportunity('one', entry_at=at(9, 2), entry_sequence=3, entry_cost='200',
                      exit_proceeds='220', reservation='203.000', reference_notional='200', quantity=2,
                      exit_at=at(9, 3), exit_sequence=5)
    two = opportunity('two', ready_at=at(9, 1), entry_at=at(9, 3), order_sequence=2,
                      entry_sequence=4, entry_cost='200', exit_proceeds='220',
                      reservation='203.000', reference_notional='200', quantity=2, exit_at=at(9, 4))
    result = replay([one, two], cash='300')

    assert result['opportunities'][0]['a_status'] == 'entered'
    assert result['opportunities'][1]['a_status'] == 'capital_blocked'
    assert result['arms']['A']['entered_count'] == 1
    assert result['arms']['A']['capital_blocked_count'] == 1


def test_exact_same_quote_exit_is_available_before_entry_only_at_the_same_sequence():
    """Reversing the exit/entry phase ordering blocks this hand-checked 200 KRW reuse."""
    first = opportunity('first', entry_cost='200', exit_proceeds='220', reservation='203.000',
                        reference_notional='200', quantity=2, exit_at=at(9, 2), exit_sequence=4)
    second = opportunity('second', ready_at=at(9, 2), entry_at=at(9, 2), order_sequence=3,
                         entry_sequence=4, entry_cost='200', exit_proceeds='210',
                         reservation='203.000', reference_notional='200', quantity=2,
                         exit_at=at(9, 3), exit_sequence=5)
    result = replay([first, second], cash='500')

    assert result['opportunities'][1]['a_status'] == 'entered'
    assert Decimal(result['arms']['A']['ending_cash']) == Decimal('530')


def test_unknown_a_outcome_does_not_turn_known_b_cash_skip_into_unknown():
    """Treating a known B skip as dependent on A's missing exit loses its known zero."""
    unknown_exit = replace(opportunity('unknown-a', gate_status='cash'), exit_at=None,
                           exit_sequence=None, exit_proceeds=None, sell_fee=None)
    result = replay([unknown_exit], cash='300')

    row = result['opportunities'][0]
    assert row['a_status'] == 'unknown'
    assert row['b_status'] == 'cash_skip'
    assert row['b_net_pnl'] == '0'
    assert result['arms']['A']['ending_cash'] is None
    assert Decimal(result['arms']['B']['ending_cash']) == Decimal('300')


def test_first_missing_seed_never_resets_from_a_later_opportunity_and_empty_dates_are_zero():
    """Seeding from a later row would fabricate a comparable cash result."""
    later = opportunity('later', date='2026-10-02', ready_at=datetime(2026, 10, 2, 9, tzinfo=KST),
                        entry_at=datetime(2026, 10, 2, 9, 1, tzinfo=KST),
                        exit_at=datetime(2026, 10, 2, 9, 2, tzinfo=KST))
    result = replay([later], dates=('2026-10-01', '2026-10-02'), cash=None)

    assert result['daily'][0] == {'date': '2026-10-01', 'a_net_pnl': '0', 'b_net_pnl': '0',
                                  'delta_net_pnl': '0', 'a_ending_cash': None, 'b_ending_cash': None}
    assert result['opportunities'][0]['a_status'] == 'unknown'
    assert result['arms']['A']['net_pnl'] is None
    assert result['initial_cash'] is None


def test_unknown_empty_date_poison_is_carried_into_the_final_arm_summary():
    """Ignoring a no-order unknown date would report a falsely complete sleeve."""
    result = replay([], dates=('2026-10-01', '2026-10-02'), unknown_dates=('2026-10-02',), cash='300')

    assert result['daily'][0]['a_net_pnl'] == '0'
    assert result['daily'][1]['a_net_pnl'] is None
    assert result['arms']['A']['net_pnl'] is None


@pytest.mark.parametrize('cash', [Decimal('NaN'), Decimal('Infinity'), True])
def test_rejects_nonfinite_or_boolean_seed(cash):
    """Accepting a non-finite seed makes every capacity comparison meaningless."""
    with pytest.raises(ValueError):
        replay_sleeve([], scheduled_dates=['2026-10-01'], unknown_dates=set(), initial_cash=cash,
                      initial_strategy_capacity=None, pending_reservation_multiplier=MULTIPLIER)


def test_per_trade_risk_is_checked_per_candidate_not_summed_across_open_positions():
    """Summing independent original risk checks would wrongly reject the second position."""
    first = opportunity('one', entry_cost='100', exit_proceeds='110', reservation='101.500',
                        reference_notional='100', capital_limits=limits(risk='11'))
    second = opportunity('two', ready_at=at(9, 1), entry_at=at(9, 1), order_sequence=1,
                         entry_sequence=3, entry_cost='100', exit_proceeds='110',
                         reservation='101.500', reference_notional='100', capital_limits=limits(risk='11'),
                         exit_at=at(9, 2))
    result = replay([first, second], cash='500')

    assert [row['a_status'] for row in result['opportunities']] == ['entered', 'entered']


def test_rejects_cross_day_exit_and_wrong_pending_reservation():
    row = opportunity('bad', exit_at=datetime(2026, 10, 2, 9, 2, tzinfo=KST))
    with pytest.raises(ValueError):
        replay([row])


def test_pending_reservation_does_not_create_a_realized_equity_drawdown():
    """Omitting pending cash from equity would turn a 609 KRW reservation into a fake loss."""
    row = opportunity('round-trip', entry_cost='600', exit_proceeds='650', reservation='609.000',
                      reference_notional='600', quantity=6, buy_fee='0', entry_price='100')
    result = replay([row], cash='1000')

    assert result['arms']['A']['realized_equity_max_drawdown'] == '0'


def test_rejects_exit_that_precedes_entry_at_the_same_timestamp():
    """An earlier sequence would otherwise run the exit before the position exists."""
    row = opportunity('backwards', entry_at=at(9, 1), entry_sequence=5,
                      exit_at=at(9, 1), exit_sequence=4)
    with pytest.raises(ValueError):
        replay([row])


def test_rejects_cost_that_does_not_include_the_declared_buy_fee():
    """Permitting mismatched cost would let the replay manufacture profit and loss."""
    with pytest.raises(ValueError):
        replay([opportunity('mismatched-cost', entry_cost='101', buy_fee='0')])


def test_unknown_quote_reserves_at_ready_but_only_poison_at_its_entry():
    """Poisoning at ready would erase an intervening fully observed round-trip."""
    first = opportunity('first', exit_at=at(9, 2), exit_sequence=4)
    unknown = replace(opportunity('missing-quote', ready_at=at(9, 3), entry_at=at(9, 6),
                                  order_sequence=5, entry_sequence=8, gate_status='unknown'),
                      entry_price=None, entry_cost=None, buy_fee=None, exit_at=None,
                      exit_sequence=None, exit_proceeds=None, sell_fee=None,
                      unknown_reason='MISSING_ENTRY_QUOTE')
    middle = opportunity('middle', ready_at=at(9, 4), entry_at=at(9, 5), order_sequence=6,
                         entry_sequence=7, exit_at=at(9, 5), exit_sequence=9)
    result = replay([first, unknown, middle], cash='1000')

    rows = {row['key']: row for row in result['opportunities']}
    assert rows['first']['a_net_pnl'] == '10'
    assert rows['middle']['a_status'] == 'entered'
    assert rows['middle']['a_net_pnl'] == '10'
    assert rows['missing-quote']['a_status'] == 'unknown'
    assert rows['missing-quote']['a_reason'] == 'MISSING_ENTRY_QUOTE'
    assert rows['missing-quote']['b_reason'] == 'MISSING_ENTRY_QUOTE'
    assert result['arms']['A']['net_pnl'] is None
    assert result['arms']['A']['unknown_key'] == 'missing-quote'
    assert result['arms']['A']['unknown_date'] == '2026-10-01'


def test_original_limit_and_shadow_capacity_are_independent_ceilings():
    """Using the original 1000 KRW residual as shadow capacity admits the second 200 KRW position."""
    first = opportunity('first', entry_cost='200', exit_proceeds='210', reservation='203.000',
                        reference_notional='200', quantity=2, exit_at=at(9, 5), exit_sequence=8,
                        capital_limits=limits(strategy='1000'))
    second = opportunity('second', ready_at=at(9, 2), entry_at=at(9, 3), order_sequence=4,
                         entry_sequence=5, entry_cost='200', exit_proceeds='210', reservation='203.000',
                         reference_notional='200', quantity=2, exit_at=at(9, 4), exit_sequence=6,
                         capital_limits=limits(strategy='1000'))
    result = replay([first, second], cash='1000', capacity='250')

    assert result['opportunities'][0]['a_status'] == 'entered'
    assert result['opportunities'][1]['a_status'] == 'capital_blocked'


def test_exact_cash_and_fees_preserve_fractional_proxy_amounts():
    """Rounding cash to the diagnostic whole-KRW PnL would lose the declared 0.45 KRW fee."""
    row = opportunity('fractional', entry_cost='100.1', buy_fee='0.1', exit_proceeds='101.65',
                      sell_fee='0.35', reservation='101.500', reference_notional='100')
    result = replay([row], cash='300')

    assert result['opportunities'][0]['a_net_pnl'] == '1.55'
    assert result['arms']['A']['total_fees'] == '0.45'
    assert result['arms']['A']['ending_cash'] == '301.550'


def test_ready_time_capital_block_keeps_missing_later_quote_as_known_zero():
    """A deterministic reservation failure must not become unknown for an unused quote."""
    row = replace(opportunity('blocked-before-quote', gate_status='allow'), entry_price=None,
                  entry_cost=None, buy_fee=None, exit_at=None, exit_sequence=None,
                  exit_proceeds=None, sell_fee=None, unknown_reason='MISSING_ENTRY_QUOTE')
    result = replay([row], cash='100')

    for arm in ('a', 'b'):
        assert result['opportunities'][0][arm + '_status'] == 'capital_blocked'
        assert result['opportunities'][0][arm + '_net_pnl'] == '0'
        assert result['opportunities'][0].get(arm + '_reason') == 'pending_reservation'
    assert result['arms']['A']['net_pnl'] == '0'
    assert Decimal(result['arms']['A']['ending_cash']) == Decimal('100')


def test_inconsistent_pending_reservation_is_rejected():
    row = opportunity('wrong-reservation', reservation='100')
    with pytest.raises(ValueError):
        replay([row])


def test_original_capital_block_releases_pending_only_at_quote_and_is_not_paired():
    """Releasing an original block at ready would admit the later 203 KRW reservation."""
    original_block = replace(opportunity('original-block', entry_at=at(9, 2), entry_sequence=4,
                                         reservation='203.000', reference_notional='200', quantity=2),
                             gate_status='capital_blocked', capital_block_reason='strategy',
                             entry_price=None, entry_cost=None, buy_fee=None, exit_at=None,
                             exit_sequence=None, exit_proceeds=None, sell_fee=None)
    later = opportunity('later', ready_at=at(9, 1), entry_at=at(9, 3), order_sequence=2,
                        entry_sequence=5, reservation='203.000', reference_notional='200', quantity=2,
                        entry_cost='200', exit_proceeds='220', exit_at=at(9, 4), exit_sequence=6)
    result = replay([original_block, later], cash='300')

    rows = {row['key']: row for row in result['opportunities']}
    assert rows['original-block']['a_status'] == 'capital_blocked'
    assert rows['original-block']['a_net_pnl'] == '0'
    assert rows['original-block']['a_reason'] == 'original_strategy'
    assert rows['later']['a_status'] == 'capital_blocked'
    assert result['paired_decisions'] == 0
    assert Decimal(result['arms']['A']['ending_cash']) == Decimal('300')


def test_capital_block_after_prior_unknown_stays_known_while_arm_keeps_first_cause():
    """Turning a declared block unknown after poison loses a deterministic zero row."""
    unknown = replace(opportunity('first-unknown', gate_status='unknown'), entry_price=None,
                      entry_cost=None, buy_fee=None, exit_at=None, exit_sequence=None,
                      exit_proceeds=None, sell_fee=None, unknown_reason='MISSING_QUOTE')
    blocked = replace(opportunity('later-block', ready_at=at(9, 2), entry_at=at(9, 3),
                                  order_sequence=3, entry_sequence=4),
                      gate_status='capital_blocked', capital_block_reason='risk', entry_price=None,
                      entry_cost=None, buy_fee=None, exit_at=None, exit_sequence=None,
                      exit_proceeds=None, sell_fee=None)
    result = replay([unknown, blocked], cash='300')

    row = {item['key']: item for item in result['opportunities']}['later-block']
    assert row['a_status'] == row['b_status'] == 'capital_blocked'
    assert row['a_net_pnl'] == row['b_net_pnl'] == '0'
    assert result['arms']['A']['net_pnl'] is None
    assert result['arms']['A']['unknown_key'] == 'first-unknown'


def test_original_ready_limit_failure_stays_known_after_prior_unknown():
    """An observed original cash limit is deterministic even when shadow cash is unknown."""
    unknown = replace(opportunity('first-unknown', gate_status='unknown'), entry_price=None,
                      entry_cost=None, buy_fee=None, exit_at=None, exit_sequence=None,
                      exit_proceeds=None, sell_fee=None)
    original_failure = opportunity('original-ready-failure', ready_at=at(9, 2), entry_at=at(9, 3),
                                   order_sequence=3, entry_sequence=4, exit_at=at(9, 4), exit_sequence=5,
                                   capital_limits=limits(cash='100'))
    result = replay([unknown, original_failure], cash='300')

    row = {item['key']: item for item in result['opportunities']}['original-ready-failure']
    assert row['a_status'] == row['b_status'] == 'capital_blocked'
    assert row['a_net_pnl'] == row['b_net_pnl'] == '0'
    assert result['arms']['A']['net_pnl'] is None


@pytest.mark.parametrize('gate',['cash','capital_blocked'])
def test_known_skip_cannot_be_declared_without_its_validated_economic_inputs(gate):
    row=replace(opportunity('incomplete-skip'),gate_status=gate,reservation=None,
                reference_notional=None,quantity=None,limits=None,entry_price=None,
                entry_cost=None,buy_fee=None,exit_at=None,exit_sequence=None,
                exit_proceeds=None,sell_fee=None,capital_block_reason='risk' if gate=='capital_blocked' else None)
    with pytest.raises(ValueError):replay([row])


def test_capital_only_gate_cannot_carry_simultaneous_entry_and_exit_economics():
    row=replace(opportunity('mixed-block-and-trade'),gate_status='capital_blocked',capital_block_reason='risk')
    with pytest.raises(ValueError):replay([row])
