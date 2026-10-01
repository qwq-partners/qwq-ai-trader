"""사전 고정 보유시간의 첫 수신 bid 평가. 주문/실제 청산 정책 재생 기능 없음."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any, Mapping

from .entry_price_shadow import KST, _integer, _mapping, _number, _text, _timestamp


@dataclass(frozen=True)
class MarkoutPolicy:
    version: str
    policy_ref: str
    fixed_at: datetime
    horizon_seconds: int
    max_delay_seconds: Decimal

    @classmethod
    def from_dict(cls, value: Any) -> MarkoutPolicy:
        data = _mapping(value, "markout_policy")
        if data.get("version") != "fixed-horizon-bid-v1":
            raise ValueError("지원하지 않는 markout_policy.version")
        if data.get("anchor", "entry_quote_observed_at") != "entry_quote_observed_at":
            raise ValueError("고정 시점의 시작 기준 불일치")
        horizon = _integer(data.get("horizon_seconds"), "markout_policy.horizon_seconds")
        if horizon > 86400:
            raise ValueError("당일 평가 범위를 넘는 horizon")
        return cls(data["version"], _text(data.get("policy_ref"), "markout_policy.policy_ref"),
                   _timestamp(data.get("fixed_at"), "markout_policy.fixed_at"), horizon,
                   _number(data.get("max_delay_seconds"), "markout_policy.max_delay_seconds", zero=True))

    def export(self) -> dict:
        return {"version": self.version, "policy_ref": self.policy_ref, "fixed_at": self.fixed_at.isoformat(),
                "horizon_seconds": self.horizon_seconds, "max_delay_seconds": str(self.max_delay_seconds),
                "anchor": "entry_quote_observed_at"}

    def evaluation_at(self, entered_at: datetime) -> datetime:
        return entered_at + timedelta(seconds=self.horizon_seconds)


def select_markout_quote(entry: Mapping, quotes: list, policy: MarkoutPolicy) -> Mapping | None:
    """가격/잔량/증거로 필터링하기 전에 첫 기록을 고른다. 불명 시각은 탐색을 막는다."""
    entered = _timestamp(entry.get("observed_at"), "entry.observed_at")
    scheduled = policy.evaluation_at(entered)
    previous_received = _timestamp(entry["provenance"].get("received_at"), "entry.received_at")
    previous_observed = entered
    for quote in quotes:
        if quote.get("symbol") != entry.get("symbol") or quote["sequence"] <= entry["sequence"]:
            continue
        provenance = _mapping(quote.get("provenance"), "markout.provenance")
        if provenance.get("tr_id") == "H0NXASP0":
            continue
        received = _timestamp(provenance.get("received_at"), "markout.received_at")
        observed = _timestamp(quote.get("observed_at"), "markout.observed_at")
        if not previous_received <= received <= observed or observed < previous_observed:
            raise ValueError("MARKOUT_QUOTE_TIME_REGRESSION")
        if received >= scheduled:
            return quote
        previous_received, previous_observed = received, observed
    return None


def evaluate_markout(record: Mapping, *, entry: Decimal, qty: int, decision: datetime,
                     entered_at: datetime, as_of: datetime, fees, price_policy,
                     policy: MarkoutPolicy) -> dict:
    """호가 전량 가격 대리치에 기존 매수/매도 비용과 슬리피지를 한 번씩 적용한다."""
    if policy.fixed_at > decision:
        raise ValueError("MARKOUT_POLICY_NOT_FIXED_AT_DECISION")
    scheduled = policy.evaluation_at(entered_at)
    local = scheduled.astimezone(KST)
    if (local.date() != entered_at.astimezone(KST).date() or local.weekday() >= 5
            or not time(9) <= local.time() < time(15, 20)):
        raise ValueError("MARKOUT_OUTSIDE_SUPPORTED_SESSION")
    if scheduled > as_of:
        raise ValueError("MARKOUT_NOT_MATURED")
    quote = _mapping(record.get("markout_quote"), "markout_quote")
    qid = _text(quote.get("quote_id"), "markout.quote_id")
    _text(quote.get("session_evidence_ref"), "markout.session_evidence_ref")
    if qid == record["quote"].get("quote_id"):
        raise ValueError("MARKOUT_REUSES_ENTRY_QUOTE")
    received = _timestamp(quote.get("received_at"), "markout.received_at")
    observed = _timestamp(quote.get("observed_at"), "markout.observed_at")
    if (quote.get("tr_id") != "H0STASP0" or type(quote.get("message_count")) is not int
            or quote["message_count"] != 1 or quote.get("source_as_of") is not None
            or quote.get("selection_rule") != "first_recorded_krx_at_or_after_horizon"
            or not scheduled <= received <= observed <= as_of):
        raise ValueError("MARKOUT_FIRST_QUOTE_PROVENANCE_OR_TIME")
    if (Decimal(str((observed - scheduled).total_seconds())) > policy.max_delay_seconds
            or Decimal(str((observed - received).total_seconds())) > price_policy.max_quote_age_seconds):
        raise ValueError("MARKOUT_QUOTE_DELAYED")
    for when in (received, observed):
        value = when.astimezone(KST)
        if (value.date() != local.date() or not time(9) <= value.time() < time(15, 20)
                or quote.get("session") != "KRX_REGULAR_CONTINUOUS"):
            raise ValueError("MARKOUT_QUOTE_SESSION_UNSUPPORTED")
    bid = _number(quote.get("bid"), "markout.bid")
    ask = _number(quote.get("ask"), "markout.ask")
    if bid > ask or _integer(quote.get("bid_size"), "markout.bid_size") < qty:
        raise ValueError("MARKOUT_QUOTE_CROSSED_OR_INSUFFICIENT_SIZE")
    sell_price = bid * (1 - price_policy.exit_slippage_bps / 10000)
    pnl, _ = fees.calculate_net_pnl(entry, sell_price, qty)
    return {"pnl": pnl, "evaluation_at": scheduled.isoformat(), "markout_quote_id": qid,
            "markout_price_proxy": str(sell_price), "markout_observed_at": observed.isoformat()}
