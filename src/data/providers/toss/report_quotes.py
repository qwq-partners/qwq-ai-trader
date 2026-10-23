"""새 보고 목적 승인에 묶인 메모리 전용 가격 전달. 계좌/파일/HTTP 접근 없음."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from .approval import ApprovedAuthority, ApprovalError
from .market_types import Quote
from .runtime_factory import command_deadline


class ReportQuotes:
    """worker의 완결 결과만 받으며 원천 시각을 갱신하지 않는다."""

    def __init__(self, authority, *, enabled):
        if type(authority) is not ApprovedAuthority or authority.plan.document['schema_version'] != 3:
            raise ApprovalError('approval_denied')
        authority.require('report_display', deadline=authority.clock() + 1)
        self._authority = authority
        self._enabled = enabled
        self._quotes = {}
        self._expires = {}
        self._ttl = authority.plan.document['report_display']['max_age_seconds']

    def clear(self):
        self._quotes = {}
        self._expires = {}

    def _allowed(self):
        try:
            if not self._enabled():
                return False
            authority = self._authority
            authority.require('report_display', deadline=authority.clock() + 1)
            authority.require('query', deadline=authority.clock() + 1)
            local = authority.now().astimezone(ZoneInfo('Asia/Seoul'))
            policy = authority.plan.document
            return (local.date().isoformat() in policy['dates'] and
                    any(s['start'] <= local.strftime('%H:%M') < s['end'] for s in policy['sessions']))
        except (ApprovalError, TypeError, ValueError):
            return False

    def _valid(self, quote, symbol):
        now = self._authority.now()
        return (type(quote) is Quote and quote.symbol == symbol and quote.currency == 'KRW'
                and quote.status == 'ok' and isinstance(quote.price, Decimal)
                and quote.price.is_finite() and quote.price > 0
                and abs(quote.price.adjusted()) <= 30 and abs(quote.price.as_tuple().exponent) <= 30
                and quote.market_basis in {'unknown', 'krx', 'krx_nxt'}
                and all(isinstance(t, datetime) and t.tzinfo is not None and t.utcoffset() is not None
                        for t in (quote.observed_at, quote.fetched_at))
                and quote.observed_at <= quote.fetched_at <= now
                and 0 <= (now - quote.observed_at).total_seconds() <= self._ttl
                and 0 <= (now - quote.fetched_at).total_seconds() <= self._ttl)

    def publish(self, result, *, command, deadline):
        # 모든 실패는 기존 메모리도 무효화한다. 늦게 도착한 future는 신선도를 연장하지 않는다.
        self.clear()
        if not self._allowed() or self._authority.clock() >= deadline:
            return False
        if (command.kind != 'prices' or result.outcome != 'success' or not result.ledger_complete
                or result.provider_failures or result.budget_skips or not result.quotes):
            return False
        try:
            command_deadline(command, self._authority.plan.document,
                             now=self._authority.now(), clock=self._authority.clock)
        except Exception:
            return False
        symbols = command.snapshot['symbols']
        quotes = {}
        for quote in result.quotes:
            if quote.symbol not in symbols or quote.symbol in quotes or not self._valid(quote, quote.symbol):
                return False
            quotes[quote.symbol] = quote
        if len(quotes) != result.observation_count:
            return False
        self._quotes = quotes
        now, tick = self._authority.now(), self._authority.clock()
        self._expires = {symbol: tick + self._ttl - (now - quote.observed_at).total_seconds()
                         for symbol, quote in quotes.items()}
        return True

    def get_quote(self, symbol):
        if not self._allowed():
            self.clear()
            return None
        quote = self._quotes.get(symbol)
        if (quote is not None and self._authority.clock() <= self._expires.get(symbol, 0)
                and self._valid(quote, symbol)):
            return quote
        self._quotes.pop(symbol, None)
        self._expires.pop(symbol, None)
        return None
