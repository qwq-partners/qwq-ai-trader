"""KST 일자 전환의 명시 증거와 순수 검증. 최초 계좌 인계 API가 아니다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

from .economics import decode_portfolio, validate_risk
from .lifecycle import TERMINAL_STATES, OrderRef
from .protection_recovery import digest
from .reservations import has_remaining_reservation

KST = ZoneInfo("Asia/Seoul")


def aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_datetime_required")
    return value.astimezone(KST)


def text(value):
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("nonempty_identifier_required")
    return value


def day(value):
    if type(value) is not str or date.fromisoformat(value).isoformat() != value:
        raise ValueError("iso_day_required")
    return value


@dataclass(frozen=True)
class DayReceipt:
    operation_id: str
    status: str
    reason: str
    committed_version: int
    fence_id: str = ""
    evidence_id: str = ""


@dataclass(frozen=True)
class RolloverFence(DayReceipt):
    account_scope: str = ""
    market: str = "KR"
    from_day: str = ""
    to_day: str = ""
    admission_generation: int = 0
    drained_ticket: int = 0
    drained_version: int = 0
    valuation_boundary: str = ""


@dataclass(frozen=True)
class ValuationPrice:
    symbol: str
    price: Decimal
    as_of: datetime
    source: str
    source_event_id: str

    def to_dict(self):
        if not isinstance(self.price, Decimal) or not self.price.is_finite() or self.price <= 0:
            raise ValueError("positive_decimal_price_required")
        return {"symbol": text(self.symbol), "price": str(self.price),
                "as_of": aware(self.as_of).isoformat(), "source": text(self.source),
                "source_event_id": text(self.source_event_id)}


@dataclass(frozen=True)
class ValuationEvidence:
    account_scope: str
    market: str
    fence_id: str
    portfolio_fingerprint: str
    valuation_boundary: datetime
    prices: tuple[ValuationPrice, ...]

    def to_dict(self):
        return {"account_scope": text(self.account_scope), "market": text(self.market),
                "fence_id": text(self.fence_id), "portfolio_fingerprint": text(self.portfolio_fingerprint),
                "valuation_boundary": aware(self.valuation_boundary).isoformat(),
                "prices": [price.to_dict() for price in self.prices]}


def portfolio_fingerprint(state):
    return digest({"positions": {symbol: {key: row[key] for key in ("quantity", "avg_price", "side")}
                                  for symbol, row in state["portfolio"]["positions"].items()},
                   "cost_basis_remaining": state["risk"]["cost_basis_remaining"],
                   "buy_fee_remaining": state["risk"]["buy_fee_remaining"]})


def scope_reason(state, account_scope):
    if not account_scope:
        return "account_scope_required"
    refs = [row.get("order_ref") for row in state.get("attempts", {}).values()]
    refs += [row.get("observation") for row in state.get("inbox", {}).values()]
    for row in refs:
        if row and (row.get("account_scope") != account_scope or row.get("market") != "KR"):
            return "account_scope_conflict"
    for root in ("lots", "cursors", "fill_identities"):
        for key in state.get(root, {}):
            try:
                ref = json.loads(key)
                if ref[0:2] != [account_scope, "KR"]:
                    return "account_scope_conflict"
            except (ValueError, TypeError, IndexError):
                return "invalid_order_scope"
    return ""


def unresolved_reason(state):
    if any(row.get("status") not in ("APPLIED", "SUPERSEDED") for row in state.get("inbox", {}).values()):
        return "unapplied_inbox"
    if state.get("protection_quote_admissions"):
        return "unresolved_quote_admission"
    for row in state.get("attempts", {}).values():
        if row.get("evidence_conflict"):
            return "evidence_conflict"
        if row.get("kind") != "submit":
            if row.get("command_status") not in ("not_sent", "rejected"):
                return "unresolved_child_command"
        elif row.get("state") not in TERMINAL_STATES:
            return "unresolved_submit"
        if row.get("observed_quantity", 0) != row.get("applied_quantity", 0):
            return "observed_not_applied"
        try:
            if has_remaining_reservation(row):
                return "remaining_reservation"
        except ValueError:
            return "invalid_reservation"
        if row.get("kind") == "submit" and row.get("observed_quantity", 0) > 0:
            try:
                cursor = state.get("cursors", {}).get(OrderRef(**row["order_ref"]).key)
                if (cursor is None or cursor["quantity"] != row["applied_quantity"]
                        or Decimal(cursor["amount"]) != Decimal(row["observed_amount"])):
                    return "observed_amount_not_applied"
            except (ValueError, KeyError, TypeError):
                return "invalid_applied_evidence"
    return ""


def validate_valuation(state, row, transition, now):
    boundary = aware(datetime.fromisoformat(row["valuation_boundary"]))
    now = aware(now)
    if (row["account_scope"] != transition["account_scope"] or row["market"] != "KR"
            or row["fence_id"] != transition["fence_id"]
            or row["valuation_boundary"] != transition["valuation_boundary"]
            or boundary > now or boundary.date().isoformat() != transition["to_day"]
            or now.date().isoformat() != transition["to_day"]):
        raise ValueError("valuation_scope_or_boundary_mismatch")
    if row["portfolio_fingerprint"] != portfolio_fingerprint(state):
        raise ValueError("valuation_portfolio_mismatch")
    prices = {}
    for quote in row["prices"]:
        as_of = aware(datetime.fromisoformat(quote["as_of"]))
        price = Decimal(quote["price"])
        if (quote["symbol"] in prices or not price.is_finite() or price <= 0
                or as_of > boundary or as_of.date() != boundary.date()
                or not text(quote["source"]) or not text(quote["source_event_id"])):
            raise ValueError("valuation_price_provenance_invalid")
        prices[quote["symbol"]] = price
    portfolio = decode_portfolio(state["portfolio"])
    if prices.keys() != portfolio.positions.keys():
        raise ValueError("valuation_price_coverage_mismatch")
    for symbol, price in prices.items():
        portfolio.positions[symbol].current_price = price
    return prices, portfolio.total_unrealized_pnl


def reset_daily(state, prices, unrealized, to_day):
    """기존 reset 필드만 변경한다. 원가/공제키/보호/원장은 그대로 둔다."""
    portfolio, risk = state["portfolio"], state["risk"]
    portfolio.update(daily_pnl="0", daily_trades=0, daily_start_unrealized_pnl=str(unrealized))
    # valuation은 별도 view 증거다. 기존 보호 replay가 참조하는 position DTO를
    # 일자 기준선 계산만을 위해 바꾸지 않는다.
    risk["day"] = risk["daily_exit_count_date"] = to_day
    risk["daily_stats"].update(date=to_day, trades=0, wins=0, losses=0, total_pnl="0",
                               max_drawdown="0", consecutive_losses=0, peak_equity=portfolio["initial_capital"])
    risk.update(consecutive_losses=0, stop_loss_today=[], stop_loss_rebound_used=[], exited_today={}, daily_exit_count=0)
    # 전일 판단 사실은 새 날에 소비될 수 없으므로 여기서 끊는다(결정 ⑩). 출처 행은
    # 이름으로 키잉돼 유계이고 as_of 당일성 검사가 재사용을 막으므로 건드리지 않는다.
    if "entry_decision_facts" in state:
        state["entry_decision_facts"] = {}
    validate_risk(risk)
    return state
