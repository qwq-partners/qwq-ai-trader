#!/usr/bin/env python3
"""Compare an explicitly supplied signal_events JSON export, without DB access."""
import argparse
import hashlib
import html
import json
import math
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analytics.rr_context_comparison import compare_rr_rows  # noqa: E402

MAX_BYTES = 32 * 1024 * 1024


class InputError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError('invalid_arguments')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError('duplicate_key')
        result[key] = value
    return result


def _float(text):
    result = float(text)
    if not math.isfinite(result):
        raise InputError('nonfinite_number')
    return result


def _constant(_text):
    raise InputError('nonfinite_number')


def _signature(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def read_input(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise InputError('invalid_file')
            if before.st_size > MAX_BYTES:
                raise InputError('input_too_large')
            data = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            if len(data) > MAX_BYTES:
                raise InputError('input_too_large')
            if len(data) != before.st_size or _signature(before) != _signature(after) or _signature(after) != _signature(os.stat(path)):
                raise InputError('input_changed')
    except OSError:
        raise InputError('file_read_failed') from None
    try:
        rows = json.loads(data, object_pairs_hook=_pairs, parse_float=_float, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise InputError('invalid_json') from None
    if type(rows) is not list or len(rows) > 10_000:
        raise InputError('invalid_rows')
    return rows, hashlib.sha256(data).hexdigest()


def _markdown(result, rendered):
    lines = ['# Offline SEPA RR context comparison', '',
             'Conditional analysis; selection fee rates and current recorded quantities are applied to both price sets.',
             'This is not an actual execution result. The original float gate is separate from the Decimal reconstruction.',
             '', '| Signal | Quantity source | Quantity | Selected RR | Current RR |',
             '| --- | --- | ---: | ---: | ---: |']
    for row in result['rows']:
        for name, lane in row['quantity_lanes'].items():
            def value(basis):
                calculation = lane[basis]
                if calculation['calculation_status'] == 'ok':
                    return calculation['quantity_adjusted']['rr']
                return calculation['calculation_reason']
            sid = html.escape(row['signal_id']).replace('|', '&#124;').replace('\n', ' ').replace('\r', ' ')
            quantity = lane['quantity'] if lane['quantity'] is not None else lane['quantity_status']
            lines.append(f'| {sid} | {name} | {quantity} | {value("selected_prices")} | {value("current_prices")} |')
    lines += ['', '## Full provenance and coverage', '',
              *('    ' + line for line in rendered.splitlines())]
    return '\n'.join(lines)


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--data-kind', required=True, choices=('actual', 'synthetic'))
    parser.add_argument('--format', choices=('json', 'markdown'), default='json')
    try:
        args = parser.parse_args(argv)
        rows, fingerprint = read_input(args.input)
        result = compare_rr_rows(rows, source_ref=f'sha256:{fingerprint}', data_kind=args.data_kind)
        result['input_sha256'] = fingerprint
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
        if args.format == 'markdown':
            print(_markdown(result, rendered))
        else:
            print(rendered)
        return 0
    except InputError as exc:
        print(f'error: {exc}', file=sys.stderr)
    except (ValueError, OverflowError, RecursionError):
        print('error: invalid_input', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
