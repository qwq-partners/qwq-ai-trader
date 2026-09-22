"""공식 저장소 현행 TTTC0081R/TTTC0084R 읽기 전용 페이지 수집 계약.

fetch(request)는 기존 호출 제한기를 소유하는, 취소 가능한 async 어댑터다.
어댑터가 GET과 인증 헤더를 구성하며 이 모듈은 HTTP/계좌 기본값/환경 접근을
갖지 않는다. 재시도나 polling을 만들지 않는다. raw rows는 일시적인 파서 입력일
뿐 로그/저장 대상이 아니며, 소비자는 안전한 필드만 명시적으로 선택해야 한다.

페이지 완결은 계좌 원자적 snapshot, 주문 부재, 취소 최종성 또는 startup 대사
성공을 증명하지 않는다. 조회 범위는 실제로 요청한 값만 기록하며 응답에서 추론하지 않는다.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import math
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping
from zoneinfo import ZoneInfo


def _freeze_json(value):
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("invalid response shape")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    if type(value) not in (str, int, bool, float, type(None)):
        raise ValueError("invalid response shape")
    if type(value) is float and not math.isfinite(value):
        raise ValueError("invalid response shape")
    return value


@dataclass(frozen=True)
class QueryRequest:
    path: str
    tr_id: str
    params: Mapping[str, str] = field(repr=False)
    tr_cont: str = ""
    method: str = field(default="GET", init=False)

    def __post_init__(self):
        params = deepcopy(dict(self.params))
        if any(type(key) is not str or type(value) is not str for key, value in params.items()):
            raise ValueError("invalid request parameters")
        object.__setattr__(self, "params", MappingProxyType(params))


@dataclass(frozen=True)
class QueryResponse:
    status_code: int
    headers: Mapping[str, str] = field(repr=False)
    body: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class QueryScope:
    account_scope: str
    query_kind: str
    tr_id: str
    start_date: str | None = None
    end_date: str | None = None
    market: str = field(default="KR", init=False)
    # 실제로 요청에 실은 거래소 범위. 요청 파라미터가 없는 조회는 "unspecified"로 남긴다.
    exchange_scope: str = "KRX"


@dataclass(frozen=True)
class QueryPage:
    started_at: datetime
    completed_at: datetime
    rows: tuple[Mapping[str, Any], ...] = field(repr=False)
    request_cont: str = field(repr=False)
    request_cursor: tuple[str, str] = field(repr=False)
    response_cont: str = field(repr=False)
    # None은 응답에 유효한 문자열 cursor 쌍이 없었다는 뜻. 값을 제조하지 않는다.
    next_cursor: tuple[str, str] | None = field(repr=False)


@dataclass(frozen=True)
class QueryCollection:
    scope: QueryScope
    started_at: datetime
    completed_at: datetime
    business_date_kst: str
    pages: tuple[QueryPage, ...]
    complete: bool
    reason: str

    @property
    def rows(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(row for page in self.pages for row in page.rows)

    @property
    def finality_supported(self) -> bool:
        return False

    @property
    def trading_permission(self) -> bool:
        return False


class LegacyExecutionQueries:
    """페이지 수/각 GET 제한 시간을 갖는 offline-injectable 단발 수집기.

    fetch는 반드시 기존 limiter를 적용해야 하고 cancellation을 삼키면 안 된다.
    request_timeout에는 limiter 대기와 요청 시간이 함께 포함된다.
    """

    def __init__(self, fetch: Callable[[QueryRequest], Awaitable[QueryResponse]], *,
                 clock: Callable[[], datetime], request_timeout: float, max_pages: int = 10):
        if (type(request_timeout) not in (int, float)
                or not math.isfinite(request_timeout) or request_timeout <= 0):
            raise ValueError("request_timeout must be positive and finite")
        if type(max_pages) is not int or max_pages <= 0:
            raise ValueError("max_pages must be a positive integer")
        if not callable(fetch) or not callable(clock):
            raise ValueError("fetch and clock must be callable")
        self._fetch, self._clock = fetch, clock
        self._request_timeout, self._max_pages = request_timeout, max_pages

    def _now(self) -> datetime:
        try:
            value = self._clock()
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError
            return value.astimezone(timezone.utc)
        except Exception:
            raise ValueError("clock must return an aware datetime") from None

    @staticmethod
    def _scope_value(value: str) -> str:
        if type(value) is not str or not value or value != value.strip():
            raise ValueError("invalid query scope")
        return value

    @staticmethod
    def _query_date(value: str) -> date:
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
            return parsed
        except (TypeError, ValueError):
            raise ValueError("invalid query date") from None

    def _credentials(self, account_number: str, product_code: str) -> dict:
        return {"CANO": self._scope_value(account_number),
                "ACNT_PRDT_CD": self._scope_value(product_code),
                "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}

    async def daily(self, *, account_scope: str, account_number: str, product_code: str,
                    start_date: str, end_date: str, exchange_scope: str = "KRX") -> QueryCollection:
        """저장소 현행 TTTC0081R 일별 주문체결 조회. EXCG_ID_DVSN_CD를 명시 송신한다.

        저장소 예제(inquire_daily_ccld.py:44,173-174)는 기본값 "KRX"를 두되 미입력도
        허용한다 — 다만 "미입력이면 KRX"라는 문장은 없으므로 미입력 동작에 기대지 않고
        직접 보낸다. 호출자는 운영 경로와 같은 "ALL"을 고를 수 있다: 범위를 넓히면 보이는
        행이 늘기만 하고 늘어난 행은 chain 판정에 들어가 종결을 더 어렵게 만든다. 그 밖의
        라벨은 실응답으로 확인하기 전까지 보내지 않는다(Q27~29).
        """
        if exchange_scope not in ("KRX", "ALL"):
            raise ValueError("unverified exchange scope")
        scope = QueryScope(self._scope_value(account_scope), "daily", "TTTC0081R", start_date, end_date,
                           exchange_scope=exchange_scope)
        start, end = self._query_date(start_date), self._query_date(end_date)
        if start > end:
            raise ValueError("invalid query date range")
        params = self._credentials(account_number, product_code)
        params.update(INQR_STRT_DT=start.isoformat().replace("-", ""), INQR_END_DT=end.isoformat().replace("-", ""),
                      SLL_BUY_DVSN_CD="00", INQR_DVSN="01", PDNO="", CCLD_DVSN="00",
                      ORD_GNO_BRNO="", ODNO="", INQR_DVSN_3="00", INQR_DVSN_1="",
                      EXCG_ID_DVSN_CD=scope.exchange_scope)
        return await self._collect(scope, "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", params, "output1")

    async def cancelable(self, *, account_scope: str, account_number: str,
                         product_code: str) -> QueryCollection:
        """저장소 현행 TTTC0084R 정정취소가능 주문 조회. 거래소 요청 파라미터가 없다.

        - 이 조회에 주문번호가 없다는 사실을 취소 확정의 근거로 쓰지 않는다. 저장소는
          '부재 = 확정'을 말하지 않고, 오히려 취소 전 psbl_qty 확인 의무만 말한다(Q12).
        - 한 번에 50건 상한이다(inquire_psbl_rvsecncl.py:39). 잘린 목록과 빈 목록을
          반환값으로 구분하지 못하는 것이 저장소 예제의 결함이므로 complete 여부로만 읽는다.
        - INQR_DVSN_1의 의미가 저장소 안에서 두 갈래다(예제 ':46 0:주문 1:종목' vs
          legacy/Sample01/kis_domstk.py:147 '정렬순서'). 어느 값이 옳은지 저장소가 판정하지
          않으므로 현행 값을 바꾸지 않는다.
        """
        scope = QueryScope(self._scope_value(account_scope), "cancelable", "TTTC0084R",
                           exchange_scope="unspecified")
        params = self._credentials(account_number, product_code)
        params.update(INQR_DVSN_1="1", INQR_DVSN_2="0")
        return await self._collect(scope, "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl", params, "output")

    @staticmethod
    def _continuation(headers: Mapping[str, str]) -> str | None:
        if not isinstance(headers, Mapping):
            return None
        normalized = {}
        for key, value in headers.items():
            if type(key) is not str or type(value) is not str:
                return None
            key = key.lower()
            if key in normalized and normalized[key] != value:
                return None
            normalized[key] = value
        continuation = normalized.get("tr_cont")
        return continuation if continuation in ("F", "M", "D", "E") else None

    @staticmethod
    def _within_legacy_window(scope: QueryScope, requested_at: datetime) -> bool:
        if scope.query_kind != "daily":
            return True
        today = requested_at.astimezone(ZoneInfo("Asia/Seoul")).date()
        # 공식 legacy 주석: 4/25 요청이면 1월~4월. 90일/동일 일자 cutoff가 아니다.
        # 이 창은 구TR legacy 주석 기준의 보수적 제약이며 신TR(TTTC0081R)의 창 정의는
        # 저장소에 없다. 좁은 쪽으로 남겨 두고 실계좌 확인 전에는 넓히지 않는다.
        year, month_index = divmod(today.year * 12 + today.month - 1 - 3, 12)
        earliest = date(year, month_index + 1, 1)
        return earliest.isoformat() <= scope.start_date <= scope.end_date <= today.isoformat()

    async def _collect(self, scope: QueryScope, path: str, params: dict, output_key: str) -> QueryCollection:
        started_at = completed_at = self._now()
        if not self._within_legacy_window(scope, started_at):
            raise ValueError("query date outside legacy month window")
        pages = []
        seen = {("", "")}
        continuation = ""
        reason, complete = "page_limit", False
        for _ in range(self._max_pages):
            try:
                page_started = self._now()
            except ValueError:
                reason = "clock_invalid"
                break
            if page_started < completed_at:
                reason = "clock_regression"
                break
            if not self._within_legacy_window(scope, page_started):
                reason = "date_outside_legacy_window"
                break
            # 각 요청을 마지막 await 전에 고정. fetch가 이전 요청을 보유해도 다음 params와 분리.
            request = QueryRequest(path, scope.tr_id, params, continuation)
            fetch_failure = None
            try:
                response = await asyncio.wait_for(self._fetch(request), timeout=self._request_timeout)
            except asyncio.TimeoutError:
                fetch_failure = "timeout"
            except asyncio.CancelledError:
                raise
            except Exception:
                fetch_failure = "fetch_failed"
            try:
                completed_at = self._now()
            except ValueError:
                reason = "clock_invalid"
                break
            if completed_at < page_started:
                reason = "clock_regression"
                break
            if fetch_failure:
                reason = fetch_failure
                break
            if not isinstance(response, QueryResponse):
                reason = "response_shape"
                break
            if type(response.status_code) is not int or response.status_code != 200:
                reason = "http_status"
                break
            if not isinstance(response.body, Mapping):
                reason = "response_shape"
                break
            if type(response.body.get("rt_cd")) is not str or response.body.get("rt_cd") != "0":
                reason = "broker_status"
                break
            next_page = self._continuation(response.headers)
            if next_page is None:
                reason = "continuation_header"
                break
            rows = response.body.get(output_key)
            if type(rows) is not list or any(type(row) is not dict for row in rows):
                reason = "rows_shape"
                break
            try:
                frozen_rows = tuple(_freeze_json(row) for row in rows)
            except (TypeError, ValueError):
                reason = "rows_shape"
                break
            cursor = (response.body.get("ctx_area_fk100"), response.body.get("ctx_area_nk100"))
            valid_cursor = all(type(value) is str for value in cursor)
            pages.append(QueryPage(page_started, completed_at, frozen_rows,
                                   request.tr_cont,
                                   (request.params["CTX_AREA_FK100"], request.params["CTX_AREA_NK100"]),
                                   next_page, cursor if valid_cursor else None))
            if next_page in ("D", "E"):
                complete, reason = True, "complete"
                break
            if not valid_cursor:
                reason = "cursor_shape"
                break
            if cursor in seen or not any(value.strip() for value in cursor):
                reason = "cursor_loop"
                break
            seen.add(cursor)
            params.update(CTX_AREA_FK100=cursor[0], CTX_AREA_NK100=cursor[1])
            continuation = "N"
        return QueryCollection(scope, started_at, completed_at,
                               started_at.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat(),
                               tuple(pages), complete, reason)
