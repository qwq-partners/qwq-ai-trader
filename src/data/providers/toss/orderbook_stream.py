"""토스 국내 통합 호가의 유한 관측. 인증·접속·자동 재접속·주문 기능 없음.

공식 AsyncAPI 1.2.2 기준. 호출자는 별도로 승인된 전용 소켓만 넘긴다.
수신 중간 유실을 검출할 sequence가 없으므로 완전한 틱 수집을 주장하지 않는다.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import re


SOURCE = "TOSS_WS_ORDERBOOK_KR"
MARKET = "KRX_NXT_CONSOLIDATED"
SCHEMA = "toss-orderbook-capture-v1"
_NUMBER = re.compile(r"[0-9]{1,20}(?:\.[0-9]{1,8})?\Z")
_ID = re.compile(r"[a-zA-Z0-9_.:-]{1,120}\Z")


def unique_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("중복 JSON 키")
        result[key] = value
    return result


def reject_json_constant(value):
    raise ValueError("유한 JSON 숫자 필요")


def decimal_string(value):
    if not isinstance(value, str) or not _NUMBER.fullmatch(value):
        raise ValueError("유한 십진 문자열 필요")
    return Decimal(value)


def aware_time(value):
    """명시한 시간대만 허용하고 UTC로 정규화한다."""
    if isinstance(value, str) and len(value) <= 80:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("시간대가 있는 시각 필요")
    return value.astimezone(timezone.utc)


def _levels(values, *, asks):
    if not isinstance(values, list) or not 1 <= len(values) <= 64:
        raise ValueError("호가 배열 범위 위반")
    parsed = []
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("호가 항목 형식 위반")
        price, volume = item.get("price"), item.get("volume")
        p, q = decimal_string(price), decimal_string(volume)
        if p <= 0 or q < 0:
            raise ValueError("음수/영 가격")
        parsed.append((p, q))
    prices = [v[0] for v in parsed]
    if prices != sorted(set(prices), reverse=not asks) or parsed[0][1] <= 0:
        raise ValueError("호가 정렬/최우선 잔량 위반")
    return parsed[0]


class TossOrderbookCapture:
    """고정한 최대 3종목, 한 연결, 한 선언만 다루는 오프라인 재생 가능 관측기."""

    def __init__(self, *, symbols, request_id, evaluation_epoch, start_at, end_at,
                 max_frames=10000, max_source_age_seconds=1):
        if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 3
                or any(not isinstance(s, str) or not re.fullmatch(r"[0-9]{6}", s) for s in symbols)
                or len(set(symbols)) != len(symbols)):
            raise ValueError("고정한 서로 다른 국내 후보 1~3개 필요")
        if any(not isinstance(v, str) or not _ID.fullmatch(v) for v in (request_id, evaluation_epoch)):
            raise ValueError("유한 관측 식별자 필요")
        self.start_at, self.end_at = aware_time(start_at), aware_time(end_at)
        if not 0 < (self.end_at - self.start_at).total_seconds() <= 3600:
            raise ValueError("1시간 이내의 양의 관측 구간 필요")
        if type(max_frames) is not int or not 1 <= max_frames <= 50000:
            raise ValueError("유한 프레임 상한 필요")
        if type(max_source_age_seconds) is not int or not 1 <= max_source_age_seconds <= 60:
            raise ValueError("명시한 원천 시각 허용 지연 필요")
        self.symbols = tuple(symbols)
        self.request_id, self.evaluation_epoch = request_id, evaluation_epoch
        self.max_frames, self.max_source_age_seconds = max_frames, max_source_age_seconds
        self.stop_reason = None
        self._claimed = False
        self._cleanup_failed = False
        self._frames = 0
        self._ack_at = None
        self._acknowledged = set()
        self._rejected = set()
        self._last_receive = self.start_at
        self._last_source = {}
        self._records = []

    def declaration(self):
        return [{"id": self.request_id}, {"type": "orderbook:kr", "codes": list(self.symbols)}]

    def stop(self, reason):
        """첫 종료 이유를 고정한다. 원문 예외/서버 메시지를 보존하지 않는다."""
        allowed = {"window_ended", "frame_limit", "receive_time_invalid", "receive_time_reversed",
                   "before_window", "frame_too_large", "invalid_frame", "invalid_ack",
                   "unacknowledged_topic", "server_error", "transport_error", "cancelled"}
        if reason not in allowed:
            raise ValueError("지원하지 않는 종료 사유")
        if self.stop_reason is None:
            self.stop_reason = reason

    def _ack(self, frame, received):
        expected = {f"orderbook:kr:{s}" for s in self.symbols}
        subscribed, rejected = frame.get("subscribed"), frame.get("rejected")
        if (self._ack_at is not None or frame.get("id") != self.request_id
                or not isinstance(subscribed, list) or not isinstance(rejected, list)
                or any(not isinstance(x, str) for x in subscribed)
                or any(not isinstance(x, dict) or not all(isinstance(x.get(k), str)
                    for k in ("target", "code", "message")) for x in rejected)):
            self.stop("invalid_ack")
            return
        denied = [x["target"] for x in rejected]
        combined = subscribed + denied
        if set(combined) != expected or len(set(combined)) != len(combined):
            self.stop("invalid_ack")
            return
        self._acknowledged = {x.rsplit(":", 1)[1] for x in subscribed}
        self._rejected = {x.rsplit(":", 1)[1] for x in denied}
        self._ack_at = received.isoformat()

    def _book(self, frame, received):
        topic = frame.get("topic")
        expected = {f"orderbook:kr:{s}" for s in self._acknowledged}
        if not isinstance(topic, str) or topic not in expected:
            self.stop("unacknowledged_topic")
            return
        symbol = topic.rsplit(":", 1)[1]
        data = frame.get("data")
        issues, source_at = [], None
        ask = bid = ask_size = bid_size = None
        if not isinstance(data, dict):
            data = {}
        raw_time = data.get("timestamp")
        if raw_time is None:
            issues.append("source_time_missing")
        else:
            try:
                source_at = aware_time(raw_time)
                age = (received - source_at).total_seconds()
                if age < 0:
                    issues.append("source_time_future")
                elif age > self.max_source_age_seconds:
                    issues.append("source_time_stale")
                prior = self._last_source.get(symbol)
                if prior is not None and source_at <= prior:
                    issues.append("source_time_not_increasing")
                # 미래 시각 하나로 후속 정상 프레임 전체를 오염시키지 않는다.
                if age >= 0 and (prior is None or source_at > prior):
                    self._last_source[symbol] = source_at
            except (ValueError, TypeError, OverflowError):
                issues.append("source_time_invalid")
        try:
            if data.get("currency") != "KRW":
                raise ValueError("통화 불일치")
            (ask, ask_size), (bid, bid_size) = _levels(data.get("asks"), asks=True), _levels(data.get("bids"), asks=False)
            if bid > ask:
                raise ValueError("역전 호가")
        except (ValueError, TypeError):
            ask = bid = ask_size = bid_size = None
            issues.append("invalid_book")
        self._records.append({
            "source": SOURCE, "market_basis": MARKET, "delivery": "LOSSY", "source_sequence": None,
            "symbol": symbol, "received_index": self._frames, "received_at": received.isoformat(),
            "source_as_of": source_at.isoformat() if source_at is not None else None,
            "ask": str(ask) if ask is not None else None, "bid": str(bid) if bid is not None else None,
            "ask_size": str(ask_size) if ask_size is not None else None,
            "bid_size": str(bid_size) if bid_size is not None else None,
            "quality_issues": issues, "kis_executable": False,
        })

    def feed(self, raw, *, received_at):
        if self.stop_reason is not None:
            return
        try:
            received = aware_time(received_at)
        except (ValueError, TypeError, OverflowError):
            self.stop("receive_time_invalid")
            return
        if received < self.start_at:
            self.stop("before_window")
            return
        if received < self._last_receive:
            self.stop("receive_time_reversed")
            return
        if received >= self.end_at:
            self.stop("window_ended")
            return
        self._last_receive = received
        self._frames += 1
        try:
            if not isinstance(raw, str):
                raise ValueError("텍스트 프레임 필요")
            if len(raw) > 65536 or len(raw.encode("utf-8")) > 65536:
                self.stop("frame_too_large")
                return
            frame = json.loads(raw, object_pairs_hook=unique_json_keys, parse_constant=reject_json_constant)
            if not isinstance(frame, dict):
                raise ValueError("JSON 객체 필요")
            kind = frame.get("type")
            if kind == "subscriptions":
                self._ack(frame, received)
            elif kind == "message":
                self._book(frame, received)
            elif kind == "error":
                self.stop("server_error")
            elif kind != "pong":
                raise ValueError("미지원 프레임")
        except (ValueError, TypeError, RecursionError, UnicodeError):
            self.stop("invalid_frame")
        if self._frames >= self.max_frames:
            self.stop("frame_limit")

    def export(self):
        return {"schema_version": SCHEMA, "evaluation_epoch": self.evaluation_epoch,
                "source": SOURCE, "market_basis": MARKET, "delivery": "LOSSY", "stream_complete": None,
                "request_id": self.request_id, "symbols": list(self.symbols),
                "start_at": self.start_at.isoformat(), "end_at": self.end_at.isoformat(),
                "max_source_age_seconds": self.max_source_age_seconds,
                "max_frames": self.max_frames, "received_frames": self._frames,
                "acknowledged_at": self._ack_at,
                "acknowledged_symbols": [s for s in self.symbols if s in self._acknowledged],
                "rejected_symbols": [s for s in self.symbols if s in self._rejected],
                "stop_reason": self.stop_reason, "cleanup_failed": self._cleanup_failed,
                "records": deepcopy(self._records)}


async def receive_orderbooks(ws, capture, *, exclusive, now=None, monotonic=None):
    """이미 인증된 전용 소켓의 소유권을 받아 유한 수신 후 닫는다.

    공유 소켓의 full-replace는 기존 구독을 없애므로 허용하지 않는다.
    이 함수는 소켓을 열거나 토큰/환경/파일을 읽지 않는다.
    """
    if exclusive is not True or not isinstance(capture, TossOrderbookCapture):
        raise ValueError("관측 전용 소켓의 명시적 소유권 필요")
    if capture._claimed or capture._frames or capture.stop_reason is not None:
        raise ValueError("새 연결에는 새 관측기가 필요")
    # 첫 await 전에 소유권을 고정해 두 수신기의 동시 진입을 막는다.
    capture._claimed = True
    now = now or (lambda: datetime.now(timezone.utc))
    monotonic = monotonic or asyncio.get_running_loop().time
    try:
        at = aware_time(now())
        if at < capture.start_at:
            capture.stop("before_window")
        if at >= capture.end_at:
            capture.stop("window_ended")
        if capture.stop_reason is not None:
            return
        deadline = monotonic() + (capture.end_at - at).total_seconds()
        next_ping = monotonic() + 60
        await asyncio.wait_for(ws.send_str(json.dumps(capture.declaration())), timeout=min(5, deadline - monotonic()))
        while capture.stop_reason is None:
            remaining = min(deadline - monotonic(), (capture.end_at - aware_time(now())).total_seconds())
            if remaining <= 0:
                capture.stop("window_ended")
                break
            if monotonic() >= next_ping:
                await asyncio.wait_for(ws.send_str("PING"), timeout=min(5, remaining))
                next_ping = monotonic() + 60
                remaining = min(deadline - monotonic(), (capture.end_at - aware_time(now())).total_seconds())
                if remaining <= 0:
                    capture.stop("window_ended")
                    break
            timeout = min(remaining, max(0.001, next_ping - monotonic()))
            try:
                raw = await asyncio.wait_for(ws.receive_str(), timeout=timeout)
            except asyncio.TimeoutError:
                continue
            capture.feed(raw, received_at=now())
    except asyncio.CancelledError:
        capture.stop("cancelled")
        raise
    except Exception:
        capture.stop("transport_error")
    finally:
        try:
            await asyncio.wait_for(ws.close(), timeout=2)
        except asyncio.CancelledError:
            capture._cleanup_failed = True
            capture.stop("cancelled")
            raise
        except Exception:
            capture._cleanup_failed = True
            capture.stop("transport_error")
