"""Pure, bounded capital replay for the offline H1 price comparison."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException
from zoneinfo import ZoneInfo

from .entry_capital_limits import CapitalLimits


KST = ZoneInfo('Asia/Seoul')
_MAX_AMOUNT = Decimal('1e15')
_CAPITAL_FAILURES = {'cash_cost', 'market', 'cash_gate', 'strategy', 'position', 'risk'}


@dataclass(frozen=True)
class ReplayOpportunity:
    key: str
    date: str
    symbol: str
    ready_at: datetime
    reservation: Decimal | None
    reference_notional: Decimal | None
    entry_at: datetime
    entry_sequence: int
    order_sequence: int
    gate_status: str
    quantity: int | None
    entry_price: Decimal | None
    entry_cost: Decimal | None
    buy_fee: Decimal | None
    limits: CapitalLimits | None
    exit_at: datetime | None
    exit_sequence: int | None
    exit_proceeds: Decimal | None
    sell_fee: Decimal | None
    unknown_reason: str | None = None
    capital_block_reason: str | None = None


@dataclass
class _Book:
    cash: Decimal | None
    strategy_capacity: Decimal | None
    pending_cash: Decimal = Decimal(0)
    pending_strategy: Decimal = Decimal(0)
    pending_symbols: dict[str, Decimal] = None
    held_cost: Decimal = Decimal(0)
    held_notional: Decimal = Decimal(0)
    held_symbols: dict[str, Decimal] = None
    open_entries: dict[str, ReplayOpportunity] = None
    realized: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    capital_seconds: Decimal = Decimal(0)
    last_at: datetime | None = None
    peak_held_cost: Decimal = Decimal(0)
    peak_open_positions: int = 0
    peak_equity: Decimal | None = None
    drawdown: Decimal = Decimal(0)
    poisoned: bool = False
    poison_reason: str | None = None
    poison_key: str | None = None
    poison_date: str | None = None
    entered_count: int = 0
    capital_blocked_count: int = 0
    price_skip_count: int = 0
    unknown_count: int = 0

    def __post_init__(self):
        self.pending_symbols = {} if self.pending_symbols is None else self.pending_symbols
        self.held_symbols = {} if self.held_symbols is None else self.held_symbols
        self.open_entries = {} if self.open_entries is None else self.open_entries
        if self.cash is not None:
            self.peak_equity = self.cash

    def advance(self, when: datetime) -> None:
        if self.poisoned:
            return
        if self.last_at is not None:
            self.capital_seconds += self.held_cost * Decimal(str((when - self.last_at).total_seconds()))
        self.last_at = when

    def mark(self) -> None:
        if self.poisoned or self.cash is None:
            return
        equity = self.cash + self.pending_cash + self.held_cost
        self.peak_equity = max(self.peak_equity, equity)
        self.drawdown = max(self.drawdown, self.peak_equity - equity)

    def poison(self, reason: str | None = None, key: str | None = None, date: str | None = None) -> None:
        if not self.poisoned:
            self.poison_reason = reason
            self.poison_key = key
            self.poison_date = date
        self.poisoned = True


def _amount(value, field: str, *, nonnegative: bool = True) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError('INVALID:replay.' + field)
    try:
        value = Decimal(value)
    except (DecimalException, TypeError, ValueError):
        raise ValueError('INVALID:replay.' + field) from None
    if not value.is_finite() or abs(value) > _MAX_AMOUNT or (nonnegative and value < 0):
        raise ValueError('INVALID:replay.' + field)
    return value


def _optional_amount(value, field: str) -> Decimal | None:
    return None if value is None else _amount(value, field)


def _sequence(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('INVALID:replay.' + field)
    return value


def _local_date(value: datetime, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('INVALID:replay.' + field)
    return value.astimezone(KST).date().isoformat()


def _date(value: str, field: str) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('INVALID:replay.' + field)
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        raise ValueError('INVALID:replay.' + field) from None
    return value


def _limits(value: CapitalLimits | None) -> CapitalLimits | None:
    if value is None:
        return None
    if not isinstance(value, CapitalLimits):
        raise ValueError('INVALID:replay.limits')
    _amount(value.cash, 'limits.cash', nonnegative=False)
    for field in ('position', 'risk', 'risk_stop'):
        _amount(getattr(value, field), 'limits.' + field)
    if value.strategy is not None:
        _amount(value.strategy, 'limits.strategy', nonnegative=False)
    if not Decimal(0) < value.risk_stop < Decimal(100):
        raise ValueError('INVALID:replay.limits.risk_stop')
    return value


def _known_entry(row: ReplayOpportunity) -> bool:
    return all(value is not None for value in
               (row.reservation, row.reference_notional, row.quantity, row.entry_price,
                row.entry_cost, row.buy_fee, row.limits))


def _known_ready(row: ReplayOpportunity) -> bool:
    return all(value is not None for value in
               (row.reservation, row.reference_notional, row.quantity, row.limits))


def _known_exit(row: ReplayOpportunity) -> bool:
    return all(value is not None for value in
               (row.exit_at, row.exit_sequence, row.exit_proceeds, row.sell_fee))


def _validate(rows, scheduled_dates, unknown_dates, initial_cash, initial_strategy_capacity, multiplier):
    if not isinstance(scheduled_dates, list):
        raise ValueError('INVALID:replay.scheduled_dates')
    dates = [_date(value, 'scheduled_date') for value in scheduled_dates]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise ValueError('INVALID:replay.scheduled_dates')
    if not isinstance(unknown_dates, set) or any(_date(value, 'unknown_date') not in dates for value in unknown_dates):
        raise ValueError('INVALID:replay.unknown_dates')
    seed = None if initial_cash is None else _amount(initial_cash, 'initial_cash')
    capacity = None if initial_strategy_capacity is None else _amount(initial_strategy_capacity, 'initial_strategy_capacity')
    multiplier = _amount(multiplier, 'pending_reservation_multiplier')
    if multiplier == 0:
        raise ValueError('INVALID:replay.pending_reservation_multiplier')
    seen = set()
    for row in rows:
        if not isinstance(row, ReplayOpportunity):
            raise ValueError('INVALID:replay.opportunity')
        if not isinstance(row.key, str) or not row.key or row.key in seen:
            raise ValueError('INVALID:replay.key')
        seen.add(row.key)
        if not isinstance(row.symbol, str) or not row.symbol:
            raise ValueError('INVALID:replay.symbol')
        if row.gate_status not in {'allow', 'cash', 'unknown', 'capital_blocked'}:
            raise ValueError('INVALID:replay.gate_status')
        if ((row.gate_status == 'capital_blocked' and row.capital_block_reason not in _CAPITAL_FAILURES)
                or (row.gate_status != 'capital_blocked' and row.capital_block_reason is not None)):
            raise ValueError('INVALID:replay.capital_block_reason')
        if row.gate_status == 'cash' and not _known_entry(row):
            raise ValueError('INVALID:replay.cash_evidence')
        if row.gate_status == 'capital_blocked' and (not _known_ready(row) or any(
                value is not None for value in (row.entry_price, row.entry_cost, row.buy_fee,
                    row.exit_at, row.exit_sequence, row.exit_proceeds, row.sell_fee))):
            raise ValueError('INVALID:replay.capital_block_evidence')
        if _date(row.date, 'date') not in dates:
            raise ValueError('INVALID:replay.date')
        if _local_date(row.ready_at, 'ready_at') != row.date or _local_date(row.entry_at, 'entry_at') != row.date:
            raise ValueError('INVALID:replay.cross_day_entry')
        _sequence(row.entry_sequence, 'entry_sequence')
        _sequence(row.order_sequence, 'order_sequence')
        if (row.ready_at, row.order_sequence) >= (row.entry_at, row.entry_sequence):
            raise ValueError('INVALID:replay.ready_order')
        if row.quantity is not None and (isinstance(row.quantity, bool) or not isinstance(row.quantity, int) or row.quantity <= 0):
            raise ValueError('INVALID:replay.quantity')
        for name in ('reservation', 'reference_notional', 'entry_price', 'entry_cost', 'buy_fee',
                     'exit_proceeds', 'sell_fee'):
            _optional_amount(getattr(row, name), name)
        if row.reference_notional is not None and _amount(row.reference_notional, 'reference_notional') <= 0:
            raise ValueError('INVALID:replay.reference_notional')
        if row.reservation is not None and _amount(row.reservation, 'reservation') <= 0:
            raise ValueError('INVALID:replay.reservation')
        _limits(row.limits)
        if row.reservation is not None and row.reference_notional is not None:
            if _amount(row.reservation, 'reservation') != _amount(row.reference_notional, 'reference_notional') * multiplier:
                raise ValueError('INVALID:replay.reservation')
        if _known_entry(row):
            reference = _amount(row.reference_notional, 'reference_notional')
            price = _amount(row.entry_price, 'entry_price')
            cost = _amount(row.entry_cost, 'entry_cost')
            fee = _amount(row.buy_fee, 'buy_fee')
            if reference <= 0 or price <= 0 or cost != price * row.quantity + fee:
                raise ValueError('INVALID:replay.entry_cost')
        if row.exit_at is not None and not _known_exit(row):
            raise ValueError('INVALID:replay.exit')
        if row.exit_at is not None:
            if (_local_date(row.exit_at, 'exit_at') != row.date
                    or (row.exit_at, row.exit_sequence) <= (row.entry_at, row.entry_sequence)):
                raise ValueError('INVALID:replay.cross_day_exit')
        if row.exit_sequence is not None:
            _sequence(row.exit_sequence, 'exit_sequence')
        if row.exit_at is None and any(value is not None for value in (row.exit_sequence, row.exit_proceeds, row.sell_fee)):
            raise ValueError('INVALID:replay.exit')
    return dates, seed, capacity


def _record(rows, row):
    return rows[row.key]


def _set_unknown(book: _Book, record: dict, arm: str, row: ReplayOpportunity) -> None:
    reason = row.unknown_reason or 'UNKNOWN_ECONOMIC_STATE'
    if record[arm + '_status'] != 'unknown':
        record[arm + '_status'] = 'unknown'
        record[arm + '_net_pnl'] = None
        record[arm + '_reason'] = reason
        book.unknown_count += 1
    book.poison(reason, row.key, row.date)


def _block(book: _Book, record: dict, arm: str, reason: str) -> None:
    record[arm + '_status'] = 'capital_blocked'
    record[arm + '_net_pnl'] = '0'
    record[arm + '_reason'] = reason
    book.capital_blocked_count += 1


def _ready(book: _Book, row: ReplayOpportunity, record: dict, arm: str) -> None:
    # A valid price skip is a known zero even if this arm was already poisoned.
    # It has no reservation to release when the prior state was unknowable.
    if _known_ready(row):
        reservation = _amount(row.reservation, 'reservation')
        reference = _amount(row.reference_notional, 'reference_notional')
        limits = row.limits
        if (reservation > limits.cash or (limits.strategy is not None and reservation > limits.strategy)
                or reference > limits.position):
            _block(book, record, arm, 'pending_reservation')
            return
    if row.gate_status == 'capital_blocked' and (book.poisoned or book.cash is None or not _known_ready(row)):
        return
    if arm == 'b' and row.gate_status == 'cash' and (book.poisoned or book.cash is None or not _known_ready(row)):
        return
    if book.poisoned or book.cash is None or not _known_ready(row):
        _set_unknown(book, record, arm, row)
        return
    if (reservation > book.cash
            or (book.strategy_capacity is not None and book.pending_strategy + book.held_notional + reservation > book.strategy_capacity)
            or book.held_symbols.get(row.symbol, Decimal(0)) + book.pending_symbols.get(row.symbol, Decimal(0)) + reference > limits.position):
        _block(book, record, arm, 'pending_reservation')
        return
    book.cash -= reservation
    book.pending_cash += reservation
    book.pending_strategy += reservation
    book.pending_symbols[row.symbol] = book.pending_symbols.get(row.symbol, Decimal(0)) + reference
    record[arm + '_pending'] = True
    book.mark()


def _release(book: _Book, row: ReplayOpportunity) -> None:
    reservation = _amount(row.reservation, 'reservation')
    reference = _amount(row.reference_notional, 'reference_notional')
    book.cash += reservation
    book.pending_cash -= reservation
    book.pending_strategy -= reservation
    remainder = book.pending_symbols.get(row.symbol, Decimal(0)) - reference
    if remainder:
        book.pending_symbols[row.symbol] = remainder
    else:
        book.pending_symbols.pop(row.symbol, None)


def _entry(book: _Book, row: ReplayOpportunity, record: dict, arm: str) -> None:
    if record[arm + '_status'] == 'capital_blocked':
        return
    if record.get(arm + '_pending'):
        _release(book, row)
        record[arm + '_pending'] = False
    if row.gate_status == 'capital_blocked':
        _block(book, record, arm, 'original_' + row.capital_block_reason)
        book.mark()
        return
    if arm == 'b' and row.gate_status == 'cash':
        record[arm + '_status'] = 'cash_skip'
        record[arm + '_net_pnl'] = '0'
        book.price_skip_count += 1
        book.mark()
        return
    if book.poisoned or book.cash is None or row.gate_status == 'unknown' or not _known_entry(row):
        _set_unknown(book, record, arm, row)
        return
    quantity = row.quantity
    notional = _amount(row.entry_price, 'entry_price') * quantity
    cost = _amount(row.entry_cost, 'entry_cost')
    limits = row.limits
    failure = limits.failure(notional, cost)
    if (failure is not None or cost > book.cash or notional * Decimal('1.3') > book.cash
            or notional * Decimal('1.001') > book.cash
            or (book.strategy_capacity is not None and book.held_notional + book.pending_strategy + notional > book.strategy_capacity)
            or (book.held_symbols.get(row.symbol, Decimal(0))
                + book.pending_symbols.get(row.symbol, Decimal(0)) + notional > limits.position)):
        _block(book, record, arm, 'original_' + failure if failure else 'shadow_capacity')
        book.mark()
        return
    book.cash -= cost
    book.held_cost += cost
    book.held_notional += notional
    book.held_symbols[row.symbol] = book.held_symbols.get(row.symbol, Decimal(0)) + notional
    book.open_entries[row.key] = row
    book.entered_count += 1
    record[arm + '_status'] = 'entered'
    record[arm + '_net_pnl'] = None
    book.peak_held_cost = max(book.peak_held_cost, book.held_cost)
    book.peak_open_positions = max(book.peak_open_positions, len(book.open_entries))
    book.mark()
    if not _known_exit(row):
        _set_unknown(book, record, arm, row)


def _exit(book: _Book, row: ReplayOpportunity, record: dict, arm: str) -> None:
    entered = book.open_entries.pop(row.key, None)
    if entered is None or book.poisoned:
        return
    cost = _amount(row.entry_cost, 'entry_cost')
    notional = _amount(row.entry_price, 'entry_price') * row.quantity
    proceeds = _amount(row.exit_proceeds, 'exit_proceeds')
    book.cash += proceeds
    book.held_cost -= cost
    book.held_notional -= notional
    remainder = book.held_symbols.get(row.symbol, Decimal(0)) - notional
    if remainder:
        book.held_symbols[row.symbol] = remainder
    else:
        book.held_symbols.pop(row.symbol, None)
    pnl = proceeds - cost
    book.realized += pnl
    book.fees += _amount(row.buy_fee, 'buy_fee') + _amount(row.sell_fee, 'sell_fee')
    record[arm + '_net_pnl'] = str(pnl)
    book.mark()


def _output_book(book: _Book) -> dict:
    poisoned = book.poisoned
    return {
        'ending_cash': None if poisoned or book.cash is None else str(book.cash),
        'net_pnl': None if poisoned else str(book.realized),
        'total_fees': None if poisoned else str(book.fees),
        'peak_held_cost': None if poisoned else str(book.peak_held_cost),
        'peak_open_positions': None if poisoned else book.peak_open_positions,
        'capital_seconds': None if poisoned else str(book.capital_seconds),
        'entered_count': book.entered_count,
        'capital_blocked_count': book.capital_blocked_count,
        'price_skip_count': book.price_skip_count,
        'unknown_count': book.unknown_count,
        'unknown_reason': book.poison_reason,
        'unknown_key': book.poison_key,
        'unknown_date': book.poison_date,
        'realized_equity_max_drawdown': None if poisoned else str(book.drawdown),
    }


def replay_sleeve(opportunities: list[ReplayOpportunity], *, scheduled_dates: list[str], unknown_dates: set[str],
                  initial_cash: Decimal | None, initial_strategy_capacity: Decimal | None,
                  pending_reservation_multiplier: Decimal) -> dict:
    """Replay two independent cash sleeves without looking ahead for a better outcome."""
    if not isinstance(opportunities, list):
        raise ValueError('INVALID:replay.opportunities')
    dates, seed, capacity = _validate(opportunities, scheduled_dates, unknown_dates, initial_cash,
                                      initial_strategy_capacity, pending_reservation_multiplier)
    books = {'a': _Book(seed, capacity), 'b': _Book(seed, capacity)}
    records = {row.key: {'key': row.key, 'date': row.date, 'symbol': row.symbol,
                         'a_status': 'pending', 'b_status': 'pending',
                         'a_net_pnl': None, 'b_net_pnl': None} for row in opportunities}
    events = []
    for row in opportunities:
        events.append((row.ready_at, row.order_sequence, 1, row.order_sequence, row.key, 'ready', row))
        events.append((row.entry_at, row.entry_sequence, 2, row.order_sequence, row.key, 'entry', row))
        if _known_exit(row):
            events.append((row.exit_at, row.exit_sequence, 0, row.order_sequence, row.key, 'exit', row))
    events.sort(key=lambda event: event[:5])
    next_date_index = 0
    for when, _, _, _, _, event_type, row in events:
        while next_date_index < len(dates) and dates[next_date_index] <= row.date:
            if dates[next_date_index] in unknown_dates:
                for book in books.values():
                    book.poison('UNKNOWN_SCHEDULED_DATE', date=dates[next_date_index])
            next_date_index += 1
        record = _record(records, row)
        for book in books.values():
            book.advance(when)
        for arm, book in books.items():
            if event_type == 'ready':
                _ready(book, row, record, arm)
            elif event_type == 'entry':
                _entry(book, row, record, arm)
            else:
                _exit(book, row, record, arm)
    while next_date_index < len(dates):
        if dates[next_date_index] in unknown_dates:
            for book in books.values():
                book.poison('UNKNOWN_SCHEDULED_DATE', date=dates[next_date_index])
        next_date_index += 1
    rows = []
    for row in opportunities:
        record = _record(records, row)
        if record['a_net_pnl'] is not None and record['b_net_pnl'] is not None:
            record['delta_net_pnl'] = str(Decimal(record['b_net_pnl']) - Decimal(record['a_net_pnl']))
        else:
            record['delta_net_pnl'] = None
        rows.append(record)
    poison_from = {}
    for arm in ('a', 'b'):
        candidates = list(unknown_dates)
        candidates.extend(row['date'] for row in rows if row[arm + '_status'] == 'unknown')
        poison_from[arm] = min(candidates) if candidates else None
    daily = []
    for date in dates:
        date_rows = [record for record in rows if record['date'] == date]
        a_unknown = poison_from['a'] is not None and date >= poison_from['a']
        b_unknown = poison_from['b'] is not None and date >= poison_from['b']
        a_pnl = None if a_unknown else str(sum((Decimal(row['a_net_pnl']) for row in date_rows), Decimal(0)))
        b_pnl = None if b_unknown else str(sum((Decimal(row['b_net_pnl']) for row in date_rows), Decimal(0)))
        daily.append({'date': date, 'a_net_pnl': a_pnl, 'b_net_pnl': b_pnl,
                      'delta_net_pnl': None if a_pnl is None or b_pnl is None else str(Decimal(b_pnl) - Decimal(a_pnl)),
                      'a_ending_cash': None, 'b_ending_cash': None})
    # Reconstruct daily ending cash from known realized PnL. A poisoned day and later days stay null.
    running = {'a': seed, 'b': seed}
    poisoned_dates = {'a': False, 'b': False}
    for day in daily:
        for arm in ('a', 'b'):
            if day[arm + '_net_pnl'] is None:
                poisoned_dates[arm] = True
                day[arm + '_ending_cash'] = None
            elif running[arm] is None or poisoned_dates[arm]:
                day[arm + '_ending_cash'] = None
            else:
                running[arm] += Decimal(day[arm + '_net_pnl'])
                day[arm + '_ending_cash'] = str(running[arm])
    a_book, b_book = books['a'], books['b']
    paired = sum(1 for row in rows if row['a_net_pnl'] is not None and row['b_net_pnl'] is not None
                 and (row['a_status'] == 'entered' or row['b_status'] == 'entered'))
    return {'arms': {'A': _output_book(a_book), 'B': _output_book(b_book)}, 'daily': daily,
            'opportunities': rows, 'paired_decisions': paired,
            'delta_net_pnl': None if a_book.poisoned or b_book.poisoned else str(b_book.realized - a_book.realized),
            'initial_cash': None if seed is None else str(seed)}
