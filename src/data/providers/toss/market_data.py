"""토스 시세 엔드포인트 래퍼 + KIS 계약 정규화 (설계 §3.1·§3.3·§4.4)

Phase 1 은 **기록만** 한다 — 여기서 나온 값은 청산·사이징·주문 어디에도 들어가지 않는다.

정규화 원칙:
- 채울 수 없는 값은 **0 이 아니라 None** (CLAUDE.md 금지 패턴). `timestamp` 가 null 이면
  `as_of=None` — 관측 시각을 지어내지 않는다 (T9/T10 시간 계약)
- 캔들은 KIS `get_daily_prices` 계약(`date`/`open`/`high`/`low`/`close`/`volume`/`value`,
  **오래된 순**)으로 맞춘다. 토스는 최신순 내림차순이라 재정렬이 필수다
- 거래대금(`value`)은 토스가 주지 않으므로 **None** — 0 을 넣으면 소비자가 "거래대금 0" 으로 읽는다
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from loguru import logger

from src.utils.session import KST

from .client import TossAPIError, TossClient

SOURCE = "toss"

MAX_SYMBOLS_PER_CALL = 200   # /prices, /market-indicators/prices 다건 상한
MAX_CANDLES_PER_CALL = 200   # /candles 1회 상한


def _to_float(value: Any) -> Optional[float]:
    """decimal 문자열 → float. 파싱 불가·부재는 None (0 으로 메우지 않는다)"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    parsed = _to_float(value)
    return None if parsed is None else int(parsed)


def _parse_ts(value: Any) -> Optional[datetime]:
    """ISO 8601 → KST aware datetime. null/파싱 불가는 None (§3.3-2)"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        logger.debug(f"[토스] timestamp 파싱 실패: {value!r}")
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


class TossMarketData:
    """토스 시세 조회 (읽기 전용)"""

    def __init__(self, client: TossClient) -> None:
        self._client = client

    # ── 현재가 ───────────────────────────────────────────────────────────────
    async def get_prices(self, symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """다건 현재가 — 200개씩 분할 조회

        Returns: `{symbol: {"price", "as_of", "currency", "source"}}`
        `/prices` 는 현재가만 준다 — 등락률·거래량·전일종가는 없다(§3.3-1).
        """
        out: Dict[str, Dict[str, Any]] = {}
        unique = list(dict.fromkeys(s for s in symbols if s))
        chunks = list(_chunks(unique, MAX_SYMBOLS_PER_CALL))
        for chunk in chunks:
            try:
                body = await self._client.get(
                    "/api/v1/prices", {"symbols": ",".join(chunk)}, group="MARKET_DATA"
                )
            except TossAPIError as e:
                if len(chunks) == 1:
                    raise                       # 단일 청크면 기존대로 전파(호출부가 실패율 기록)
                logger.warning(f"[토스] 현재가 청크 실패 — 건너뜀 ({len(chunk)}종목): {e}")
                continue                        # 다중 청크: 성공분은 살린다(응답에 없는 심볼 = 실패)
            for row in body.get("result") or []:
                symbol = row.get("symbol")
                if not symbol:
                    continue
                as_of = _parse_ts(row.get("timestamp"))
                out[str(symbol)] = {
                    "price": _to_float(row.get("lastPrice")),
                    "as_of": as_of,
                    "currency": row.get("currency"),
                    "data_status": "ok" if as_of is not None else "partial",   # 관측 시각 모르면 partial(§3.3-2)
                    "source": SOURCE,
                }
        return out

    # ── 캔들 ─────────────────────────────────────────────────────────────────
    async def get_candles(
        self,
        symbol: str,
        interval: str = "1d",
        count: int = 100,
        adjusted: bool = True,
    ) -> List[Dict[str, Any]]:
        """캔들 — KIS `get_daily_prices` 계약으로 정규화해 **오래된 순**으로 반환

        행: `{"date": "YYYYMMDD", "open"/"high"/"low"/"close": float, "volume": int,
              "value": None, "timestamp": datetime}`
        `timestamp`(KST aware)는 KIS 계약에 없는 추가 키다 — 1분봉이 날짜만으로는
        구분되지 않으므로 항상 함께 싣는다(일봉 소비자는 무시하면 된다).
        200봉 초과는 `nextBefore` 페이징.
        """
        collected: Dict[datetime, Dict[str, Any]] = {}
        before: Optional[str] = None
        max_pages = (max(count, 1) + MAX_CANDLES_PER_CALL - 1) // MAX_CANDLES_PER_CALL + 1

        for _ in range(max_pages):
            params: Dict[str, Any] = {
                "symbol": symbol,
                "interval": interval,
                "count": min(count, MAX_CANDLES_PER_CALL),
                "adjusted": adjusted,
            }
            if before is not None:
                params["before"] = before
            body = await self._client.get(
                "/api/v1/candles", params, group="MARKET_DATA_CHART"
            )
            result = body.get("result") or {}
            rows = result.get("candles") or []
            for row in rows:
                ts = _parse_ts(row.get("timestamp"))
                close = _to_float(row.get("closePrice"))
                if ts is None or close is None:
                    continue
                collected[ts] = {          # before 가 inclusive라 경계 봉이 겹친다 → 시각 키로 dedup
                    "date": ts.strftime("%Y%m%d"),
                    "open": _to_float(row.get("openPrice")),
                    "high": _to_float(row.get("highPrice")),
                    "low": _to_float(row.get("lowPrice")),
                    "close": close,
                    "volume": _to_int(row.get("volume")),
                    "value": None,          # 토스 미제공 — 0 금지
                    "timestamp": ts,
                }
            prev_before = before
            before = result.get("nextBefore")
            if not rows or before is None or before == prev_before or len(collected) >= count:
                break   # before 가 전진하지 않으면 같은 페이지를 상한까지 재요청하게 된다

        ordered = [collected[ts] for ts in sorted(collected)]   # 최신순 → 오래된 순
        return ordered[-count:] if count > 0 else ordered

    # ── 호가 ─────────────────────────────────────────────────────────────────
    async def get_orderbook(self, symbol: str) -> Dict[str, Any]:
        body = await self._client.get(
            "/api/v1/orderbook", {"symbol": symbol}, group="MARKET_DATA"
        )
        result = body.get("result") or {}

        def _levels(key: str, *, reverse: bool) -> List[Dict[str, Optional[float]]]:
            rows = [
                {"price": _to_float(e.get("price")), "volume": _to_int(e.get("volume"))}
                for e in (result.get(key) or [])
            ]
            rows = [r for r in rows if r["price"] is not None]
            # index 0 = 최우선호가를 코드가 보장한다 — asks 오름차순, bids 내림차순(서버 순서 의존 금지)
            return sorted(rows, key=lambda r: r["price"], reverse=reverse)

        return {
            "symbol": symbol,
            "as_of": _parse_ts(result.get("timestamp")),
            "currency": result.get("currency"),
            "asks": _levels("asks", reverse=False),
            "bids": _levels("bids", reverse=True),
            "source": SOURCE,
        }

    # ── 상/하한가 ────────────────────────────────────────────────────────────
    async def get_price_limits(self, symbol: str) -> Dict[str, Any]:
        body = await self._client.get(
            "/api/v1/price-limits", {"symbol": symbol}, group="MARKET_DATA"
        )
        result = body.get("result") or {}
        return {
            "symbol": symbol,
            "as_of": _parse_ts(result.get("timestamp")),
            "upper_limit": _to_float(result.get("upperLimitPrice")),
            "lower_limit": _to_float(result.get("lowerLimitPrice")),
            "currency": result.get("currency"),
            "source": SOURCE,
        }

    # ── 장운영 캘린더 ────────────────────────────────────────────────────────
    async def get_market_calendar_kr(self, date: Optional[str] = None) -> Dict[str, Any]:
        """국내 장운영 정보 (KRX+NXT 통합)

        응답 원형(`today`/`previousBusinessDay`/`nextBusinessDay`)을 그대로 실어 보낸다 —
        Phase 1 은 `utils/session.py` 하드코딩과 **대조·경고**만 하고 세션 판정을 바꾸지 않는다(§6.3).
        """
        body = await self._client.get(
            "/api/v1/market-calendar/KR",
            {"date": date} if date else None,
            group="MARKET_INFO",
        )
        result = body.get("result")
        return {
            "calendar": result if isinstance(result, dict) else None,
            "source": SOURCE,
        }

    # ── 지수 현재가 ──────────────────────────────────────────────────────────
    async def get_index_prices(self, symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """시장 지표 현재가 — `timestamp` 가 null 로 오는 것이 실측 기본값이라
        `as_of=None` 을 그대로 둔다. 소비자는 `data_status=partial` 로 다뤄야 한다(§3.3-2).
        """
        out: Dict[str, Dict[str, Any]] = {}
        unique = list(dict.fromkeys(s for s in symbols if s))
        for chunk in _chunks(unique, MAX_SYMBOLS_PER_CALL):
            body = await self._client.get(
                "/api/v1/market-indicators/prices",
                {"symbols": ",".join(chunk)},
                group="MARKET_INDICATOR",
            )
            for row in body.get("result") or []:
                symbol = row.get("symbol")
                if not symbol:
                    continue
                as_of = _parse_ts(row.get("timestamp"))
                out[str(symbol)] = {
                    "price": _to_float(row.get("lastPrice")),
                    "as_of": as_of,
                    "data_status": "ok" if as_of is not None else "partial",   # 실측 기본값 null → partial
                    "source": SOURCE,
                }
        return out
