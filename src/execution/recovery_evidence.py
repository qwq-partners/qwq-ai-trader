"""Offline consistency checks. No account access, repairs, replay or release API."""

from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, localcontext
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo


KINDS = {"broker": "broker_all_orders", "journal": "journal_events",
         "baseline": "baseline_snapshots"}
KST = ZoneInfo("Asia/Seoul")
MAX_ROWS = 100_000


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError("invalid text")
    return value


def _integer(value):
    if type(value) is not int or not 0 <= value <= 10**12:
        raise ValueError("invalid quantity")
    return value


def _number(value):
    # Fixed-point input keeps money exact and avoids unbounded Decimal exponents.
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,16}(?:\.\d{1,28})?", value, re.ASCII):
        raise ValueError("invalid decimal string")
    return Decimal(value)


def _day(value, compact=False):
    pattern = r"[0-9]{8}" if compact else r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("invalid date")
    return date.fromisoformat(value)


def _time(value):
    _text(value)
    value = datetime.fromisoformat(value)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone required")
    return value


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _deltas(counter):
    return [{"quantity": qty, "price": str(price), "count": count}
            for (qty, price), count in sorted(counter.items())]


def _envelope(value, kind):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported envelope")
    if value.get("market") != "KR" or value.get("source_kind") != KINDS[kind]:
        raise ValueError("wrong evidence kind or market")
    _text(value["account_scope"])
    _text(value["source_ref"])
    start, end = _day(value["window_start"]), _day(value["window_end"])
    if start > end or _time(value["captured_at"]).astimezone(KST).date() < end:
        raise ValueError("invalid coverage chronology")
    if type(value["declared_complete"]) is not bool or not isinstance(value["rows"], list) or len(value["rows"]) > MAX_ROWS:
        raise ValueError("invalid evidence rows or completeness")


def _broker(row):
    day = _day(row["ord_dt"], compact=True).isoformat()
    odno = _text(row["odno"])
    symbol = _text(row["pdno"])
    side = {"01": "sell", "02": "buy"}[row["sll_buy_dvsn_cd"]]
    numbers = {}
    for key in ("ord_qty", "tot_ccld_qty", "cnc_cfrm_qty", "rmn_qty", "rjct_qty"):
        value = row[key]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,13}", value):
            raise ValueError("invalid raw quantity")
        numbers[key] = _integer(int(value))
    qty, filled = numbers["ord_qty"], numbers["tot_ccld_qty"]
    if qty <= 0 or filled + numbers["cnc_cfrm_qty"] + numbers["rmn_qty"] + numbers["rjct_qty"] != qty:
        raise ValueError("inconsistent order quantities")
    average = _number(row["avg_prvs"])
    if (filled > 0 and average <= 0) or (filled == 0 and average != 0):
        raise ValueError("inconsistent average")
    if row["cncl_yn"] not in {"Y", "N"} or not isinstance(row["orgn_odno"], str):
        raise ValueError("invalid order lineage")
    terminal = filled == qty or (row["cncl_yn"] == "Y" and numbers["rmn_qty"] == 0
                                 and numbers["rjct_qty"] == 0 and filled + numbers["cnc_cfrm_qty"] == qty)
    return {"key": (day, odno), "symbol": symbol, "side": side, "quantity": qty,
            "filled": filled, "average": average, "terminal": terminal,
            "lineage": row["orgn_odno"] not in {"", "0", "0000000000"}}


def _journal(row):
    day = _day(row["event_date"]).isoformat()
    event_id, trade_id, symbol = (_text(row[key]) for key in ("event_id", "trade_id", "symbol"))
    if row["market"] != "KR" or row["date_basis"] != "Asia/Seoul" or row["side"] not in {"buy", "sell"}:
        raise ValueError("invalid journal identity")
    qty, price = _integer(row["quantity"]), _number(row["price"])
    if qty == 0 or price <= 0:
        raise ValueError("nonpositive journal execution")
    odno = row["kis_order_no"]
    if odno is not None:
        _text(odno)
    return {"key": (day, odno), "symbol": symbol, "side": row["side"],
            "quantity": qty, "price": price, "event_id": event_id, "trade_id": trade_id}


def _baseline(row):
    if row["owner"] not in {"broker", "internal"} or not isinstance(row["positions"], list) or len(row["positions"]) > MAX_ROWS:
        raise ValueError("invalid snapshot")
    positions = {}
    for position in row["positions"]:
        symbol = _text(position["symbol"])
        if symbol in positions:
            raise ValueError("duplicate position")
        positions[symbol] = _integer(position["quantity"])
    cash = row["cash"]
    if cash["basis"] not in {"orderable", "deposit", "settled", "portfolio", "unknown"}:
        raise ValueError("invalid cash basis")
    amount = None if cash["amount"] is None else _number(cash["amount"])
    return {"key": row["owner"], "captured_at": _time(row["captured_at"]),
            "positions": positions, "cash": amount, "cash_basis": cash["basis"]}


def reconcile_execution_evidence(ledger, broker, journal, baseline):
    """Compare declared offline evidence; ValueError for unusable envelopes.

    The ledger export must come from read_execution_ledger's verified event replay.
    Digests identify content, never authenticate declarations or baseline inclusion.
    """
    try:
        with localcontext() as context:
            context.prec = 80
            return _reconcile(ledger, broker, journal, baseline)
    except (KeyError, TypeError, ArithmeticError, OverflowError) as exc:
        raise ValueError("unusable evidence structure") from exc


def _reconcile(ledger, broker, journal, baseline):
    if (ledger.get("format") != "execution-ledger-export-v1" or type(ledger.get("schema_version")) is not int
            or ledger["schema_version"] != 1 or not isinstance(ledger.get("sessions"), dict)
            or not isinstance(ledger.get("orders"), dict)):
        raise ValueError("invalid ledger export")
    scope = _text(ledger["account_scope"])
    _integer(ledger["event_count"])
    if not isinstance(ledger["source_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", ledger["source_sha256"]):
        raise ValueError("invalid source digest")
    if ledger["state_digest"] != _digest({key: ledger[key] for key in ("sessions", "orders")}):
        raise ValueError("export state digest mismatch")
    sources = {"broker": broker, "journal": journal, "baseline": baseline}
    for name, source in sources.items():
        _envelope(source, name)
    window = (broker["window_start"], broker["window_end"])
    issues, inputs, parsed = [], {}, {}

    def issue(code, source, severity="incomplete", **details):
        issues.append({"code": code, "source": source, "severity": severity, **details})

    for name, source in sources.items():
        same_scope = source["account_scope"] == scope
        same_window = (source["window_start"], source["window_end"]) == window
        if not same_scope:
            issue("scope_mismatch", name, "mismatch")
        if not same_window:
            issue("window_mismatch", name)
        if not source["declared_complete"]:
            issue("declared_incomplete", name)
        stats = {"row_count": len(source["rows"]), "valid_rows": 0, "invalid_rows": 0,
                 "duplicate_rows": 0, "content_sha256": _digest(source),
                 "source_ref": source["source_ref"], "captured_at": source["captured_at"],
                 "window_start": source["window_start"], "window_end": source["window_end"],
                 "declared_complete": source["declared_complete"], "provenance": "declared"}
        inputs[name], groups, seen = stats, defaultdict(list), set()
        parser = {"broker": _broker, "journal": _journal, "baseline": _baseline}[name]
        for index, raw in enumerate(source["rows"]):
            try:
                if not isinstance(raw, dict):
                    raise ValueError("invalid row")
                row = parser(raw)
                if name != "baseline" and not source["window_start"] <= row["key"][0] <= source["window_end"]:
                    raise ValueError("outside declared window")
                if name == "baseline" and (row["captured_at"] > _time(source["captured_at"])
                        or row["captured_at"].astimezone(KST).date() < _day(source["window_end"])):
                    raise ValueError("invalid snapshot chronology")
            except (KeyError, ValueError, TypeError, ArithmeticError):
                stats["invalid_rows"] += 1
                issue("invalid_row", name, row_index=index)
                continue
            stats["valid_rows"] += 1
            identity = row["event_id"] if name == "journal" else row["key"]
            if identity in seen:
                stats["duplicate_rows"] += 1
                issue("duplicate_identity", name, "mismatch", row_index=index)
            seen.add(identity)
            if name == "journal" and row["key"][1] is None:
                issue("journal_identity_missing", name, row_index=index)
            if same_scope and same_window:
                groups[row["key"]].append(row)
        parsed[name] = groups

    unclean = [key for key, value in ledger["sessions"].items() if not value["clean"] or value["prior_unclean"]]
    if unclean:
        issue("unclean_session_window_unknown", "ledger")
    results, covered, ignored = [], set(), 0
    for key, order in ledger["orders"].items():
        facts = order["facts"]
        day = _day(facts["order_date"], compact=True).isoformat()
        resolved = order["status"] in {"not_sent", "rejected"} or (
            order["status"] in {"accepted", "canceled"} and order["terminal_quantity"] is not None
            and all(item["handoff_returned"] for item in order["executions"]))
        if not window[0] <= day <= window[1]:
            ignored += 1
            if not resolved:
                issue("unresolved_outside_window", "ledger", order_key=key)
            continue
        if order["status"] in {"not_sent", "rejected"}:
            continue
        if not order["odno"]:
            issue("ledger_identity_missing", "ledger", order_key=key)
            continue
        identity = (day, order["odno"])
        covered.add(identity)
        result = {"order_key": key, "order_date": day, "odno": order["odno"],
                  "symbol": facts["symbol"], "side": facts["side"],
                  "ledger_quantity": order["observed_quantity"], "broker_quantity": None,
                  "broker_terminal": None, "broker_implied_notional": None}
        if not resolved:
            issue("ledger_not_resolved", "ledger", order_key=key)
        rows = parsed["broker"].get(identity, [])
        if not rows:
            issue("broker_order_missing", "broker", order_key=key)
        elif len(rows) == 1:
            row = rows[0]
            result.update(broker_quantity=row["filled"], broker_terminal=row["terminal"],
                          broker_implied_notional=str(row["average"] * row["filled"]))
            if (row["symbol"], row["side"], row["quantity"]) != (facts["symbol"], facts["side"], facts["quantity"]):
                issue("broker_identity_mismatch", "broker", "mismatch", order_key=key)
            if row["lineage"]:
                issue("unsupported_lineage", "broker", order_key=key)
            if not row["terminal"]:
                issue("broker_not_terminal", "broker", order_key=key)
            if row["filled"] != order["observed_quantity"] or row["average"] != Decimal(order["observed_average"]):
                issue("broker_execution_mismatch", "broker", "mismatch", order_key=key)
        expected = Counter((item["to_quantity"] - item["from_quantity"], round(Decimal(item["price"]), 2))
                           for item in order["executions"])
        supplied = Counter()
        for row in parsed["journal"].get(identity, []):
            if (row["symbol"], row["side"]) != (facts["symbol"], facts["side"]):
                issue("journal_identity_mismatch", "journal", "mismatch", order_key=key)
            supplied[(row["quantity"], row["price"])] += 1
        expected_qty = sum(qty * count for (qty, _), count in expected.items())
        actual_qty = sum(qty * count for (qty, _), count in supplied.items())
        expected_amount = sum((qty * price * count for (qty, price), count in expected.items()), Decimal(0))
        actual_amount = sum((qty * price * count for (qty, price), count in supplied.items()), Decimal(0))
        result.update(expected_journal_rows=sum(expected.values()), supplied_journal_rows=sum(supplied.values()),
                      expected_journal_quantity=expected_qty, supplied_journal_quantity=actual_qty,
                      expected_journal_notional=str(expected_amount), supplied_journal_notional=str(actual_amount),
                      journal_quantity_match=expected_qty == actual_qty,
                      journal_notional_match=expected_amount == actual_amount, journal_shape_match=expected == supplied,
                      missing_journal_deltas=_deltas(expected - supplied), extra_journal_deltas=_deltas(supplied - expected))
        if expected != supplied:
            issue("journal_delta_mismatch", "journal", "mismatch", order_key=key)
        results.append(result)
    for name in ("broker", "journal"):
        for identity in parsed[name]:
            if identity not in covered:
                issue("unattributed_" + name + "_order", name, order_date=identity[0], odno=identity[1])
    # A trade can span buy/sell orders, but cannot change market instrument.
    trades = defaultdict(set)
    for rows in parsed["journal"].values():
        for row in rows:
            trades[row["trade_id"]].add(row["symbol"])
    if any(len(symbols) > 1 for symbols in trades.values()):
        issue("journal_trade_identity_conflict", "journal", "mismatch")

    base = {"position_match": None, "position_differences": None, "cash_match": None,
            "cash_flow_reconstruction": "unavailable"}
    snapshots = parsed["baseline"]
    if any(len(snapshots.get(owner, [])) != 1 for owner in ("broker", "internal")):
        issue("baseline_pair_missing_or_ambiguous", "baseline")
    else:
        left, right = snapshots["broker"][0], snapshots["internal"][0]
        base["position_differences"] = [
            {"symbol": symbol, "broker_quantity": left["positions"].get(symbol, 0),
             "internal_quantity": right["positions"].get(symbol, 0)}
            for symbol in sorted(left["positions"].keys() | right["positions"].keys())
            if left["positions"].get(symbol, 0) != right["positions"].get(symbol, 0)]
        base["position_match"] = not base["position_differences"]
        base["captured_at"] = {"broker": left["captured_at"].isoformat(), "internal": right["captured_at"].isoformat()}
        base["cash_values"] = {owner: {"amount": str(row["cash"]) if row["cash"] is not None else None,
                                      "basis": row["cash_basis"]}
                               for owner, row in (("broker", left), ("internal", right))}
        if not base["position_match"]:
            issue("baseline_position_mismatch", "baseline", "mismatch")
        if left["captured_at"] != right["captured_at"]:
            issue("baseline_time_difference", "baseline")
        if left["cash"] is None or right["cash"] is None:
            issue("cash_unavailable", "baseline")
        elif left["cash_basis"] != right["cash_basis"] or left["cash_basis"] == "unknown":
            issue("cash_basis_mismatch", "baseline")
        else:
            base["cash_match"] = left["cash"] == right["cash"]
            if not base["cash_match"]:
                issue("baseline_cash_mismatch", "baseline", "mismatch")
    status = "mismatch" if any(item["severity"] == "mismatch" for item in issues) else "incomplete" if issues else "consistent"
    return {"format": "kr-recovery-evidence-v1", "account_scope": scope,
            "window_start": window[0], "window_end": window[1], "comparison_status": status,
            "inputs": {"ledger": {name: ledger[name] for name in ("event_count", "state_digest", "source_sha256")}, **inputs},
            "orders": results, "orders_outside_window": ignored, "unclean_sessions": unclean,
            "session_risk_window": "unverified", "baseline": base, "issues": issues,
            "next_actions": [
                {"priority": "P0", "source": source, "resolve_codes": sorted({item["code"] for item in issues if item["source"] == source})}
                for source in ("ledger", "broker", "journal", "baseline") if any(item["source"] == source for item in issues)
            ] + [{"priority": "P0", "source": "account", "required_evidence":
                  "independent opening baseline, all account movements and broker inclusion cutoff; human recovery review"}],
            "runtime_release_allowed": False, "replay_allowed": False, "state_mutated": False,
            "baseline_inclusion": "unverified"}
