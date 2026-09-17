"""실제 GET/공용 limiter/수집기를 합성 HTTP 경계로 통합 검증한다."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
import pytest

from src.execution.broker import kis_kr
from src.execution.broker.kis_kr import KISBroker, KISConfig
from src.utils import kis_rate_limit


NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.value = 100.0
        self.waiting = asyncio.Event()
        self.block = False

    async def sleep(self, delay):
        self.waiting.set()
        if self.block:
            await asyncio.Event().wait()
        self.value += delay + .000001
        await asyncio.sleep(0)


class Response:
    def __init__(self, *, status=200, continuation="E", rows=None, cursor=("", ""),
                 kind="daily", body=None, block=False, error=None):
        self.status = status
        self.headers = CIMultiDictProxy(CIMultiDict(
            [] if continuation is None else [("tr_cont", continuation)]))
        self.body = body if body is not None else {
            "rt_cd": "0", "output1" if kind == "daily" else "output": rows or [],
            "ctx_area_fk100": cursor[0], "ctx_area_nk100": cursor[1],
        }
        self.block, self.error = block, error
        self.entered = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        if self.block:
            await asyncio.Event().wait()
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.body


class Session:
    closed = False

    def __init__(self, responses, clock):
        self.responses, self.clock, self.requests = list(responses), clock, []

    def get(self, url, *, headers, params):
        self.requests.append({"url": url, "headers": dict(headers), "params": dict(params),
                              "sent_at": self.clock.value})
        return self.responses.pop(0)


class Tokens:
    _access_token = "SYNTHETIC_TOKEN"

    def __init__(self):
        self.invalidations = 0

    async def get_access_token(self):
        self._access_token = "SYNTHETIC_REFRESHED_TOKEN"
        return self._access_token

    def invalidate(self):
        self.invalidations += 1
        self._access_token = None

    def _is_token_valid(self):
        return True


@pytest.fixture
def setup_broker(monkeypatch):
    clock = Clock()
    kis_rate_limit.reset()
    monkeypatch.setattr(kis_rate_limit, "time", SimpleNamespace(monotonic=lambda: clock.value))
    monkeypatch.setattr(kis_rate_limit, "asyncio", SimpleNamespace(sleep=clock.sleep))
    monkeypatch.setattr(kis_kr, "asyncio", SimpleNamespace(sleep=clock.sleep, TimeoutError=asyncio.TimeoutError))

    def make(responses, *, env="prod", timeout=15):
        tokens = Tokens()
        broker = KISBroker(KISConfig(app_key="SYNTHETIC_KEY", app_secret="SYNTHETIC_SECRET",
                                    account_no="SYNTHETIC_ACCOUNT", account_product_cd="SYNTHETIC_PRODUCT",
                                    env=env, timeout_seconds=timeout), token_manager=tokens)
        broker._token = tokens._access_token
        broker._session = Session(responses, clock)
        return broker, broker._session

    yield make, clock
    kis_rate_limit.reset()


async def collect(broker, kind="daily"):
    if kind == "cancelable":
        return await broker.get_execution_cancelable(account_scope="test-scope", clock=lambda: NOW)
    return await broker.get_execution_daily(account_scope="test-scope", start_date="2026-09-18",
                                            end_date="2026-09-18", clock=lambda: NOW)


@pytest.mark.parametrize("kind,tr,path", [
    ("daily", "TTTC8001R", "inquire-daily-ccld"),
    ("cancelable", "TTTC8036R", "inquire-psbl-rvsecncl"),
])
def test_real_get_collects_pages_and_applies_shared_ledger_interval(setup_broker, kind, tr, path):
    make, _ = setup_broker
    broker, session = make([Response(continuation="F", rows=[{"odno": "one"}], cursor=("F1", "N1"), kind=kind),
                            Response(rows=[{"odno": "two"}], kind=kind)])
    result = asyncio.run(collect(broker, kind))
    assert result.complete and len(result.pages) == 2
    assert [row["odno"] for row in result.rows] == ["one", "two"]
    assert not result.trading_permission and not result.finality_supported
    first, second = session.requests
    assert first["url"].endswith("/" + path)
    assert first["headers"]["tr_id"] == tr and first["headers"]["tr_cont"] == ""
    assert first["params"]["CANO"] == "SYNTHETIC_ACCOUNT"
    assert first["params"]["ACNT_PRDT_CD"] == "SYNTHETIC_PRODUCT"
    assert second["headers"]["tr_cont"] == "N"
    assert (second["params"]["CTX_AREA_FK100"], second["params"]["CTX_AREA_NK100"]) == ("F1", "N1")
    assert second["sent_at"] - first["sent_at"] >= 1.05
    assert kis_rate_limit._state["ledger_busy_since"] == 0


@pytest.mark.parametrize("status,continuation,reason", [(200, None, "continuation_header"),
                                                       (201, "E", "http_status"),
                                                       (403, "E", "http_status")])
def test_real_status_and_headers_cannot_be_manufactured_as_success(setup_broker, status, continuation, reason):
    make, _ = setup_broker
    broker, session = make([Response(status=status, continuation=continuation)])
    result = asyncio.run(collect(broker))
    assert not result.complete and result.reason == reason and not result.pages
    assert len(session.requests) == 1


@pytest.mark.parametrize("headers,complete", [
    ([("tr_cont", "E"), ("tr_cont", "E")], True),
    ([("Tr_Cont", "E"), ("TR_CONT", "E")], True),
    ([("tr_cont", "E"), ("tr_cont", "F")], False),
    ([("Tr_Cont", "E"), ("TR_CONT", "F")], False),
])
def test_aiohttp_header_duplicates_are_validated_before_mapping_collapse(setup_broker, headers, complete):
    make, _ = setup_broker
    response = Response()
    response.headers = CIMultiDictProxy(CIMultiDict(headers))
    broker, _ = make([response])
    result = asyncio.run(collect(broker))
    assert result.complete is complete
    assert not result.trading_permission and not result.finality_supported
    if not complete:
        assert not result.pages


def test_second_page_http_retry_exhaustion_preserves_first_page_and_next_acquire(setup_broker):
    make, _ = setup_broker
    broker, session = make([Response(continuation="F", rows=[{"odno": "one"}], cursor=("F1", "N1")),
                            *[Response(status=503) for _ in range(3)], Response(kind="cancelable")])
    async def scenario():
        result = await collect(broker)
        assert not result.complete and result.reason == "http_status"
        assert [row["odno"] for row in result.rows] == ["one"]
        assert len(session.requests) == 4
        assert all(request["headers"]["tr_cont"] == "N" for request in session.requests[1:])
        assert kis_rate_limit._state["ledger_busy_since"] == 0
        assert (await collect(broker, "cancelable")).complete
    asyncio.run(scenario())


@pytest.mark.parametrize("error", [aiohttp.ClientConnectionError("synthetic"), asyncio.TimeoutError()])
def test_network_failure_releases_actual_acquisition_and_can_query_again(setup_broker, error):
    make, _ = setup_broker
    broker, session = make([*[Response(error=error) for _ in range(3)], Response()])
    async def scenario():
        result = await collect(broker)
        assert not result.complete and result.reason == "fetch_failed"
        assert len(session.requests) == 3 and kis_rate_limit._state["ledger_busy_since"] == 0
        assert (await collect(broker)).complete
    asyncio.run(scenario())


def test_config_timeout_cancels_inflight_get_and_releases_ledger(setup_broker):
    make, _ = setup_broker
    broker, session = make([Response(block=True), Response()], timeout=.01)
    async def scenario():
        result = await collect(broker)
        assert not result.complete and result.reason == "timeout"
        assert len(session.requests) == 1 and kis_rate_limit._state["ledger_busy_since"] == 0
        assert (await collect(broker)).complete
    asyncio.run(scenario())


@pytest.mark.parametrize("through_collector", [False, True])
def test_caller_cancellation_releases_inflight_get_and_propagates(setup_broker, through_collector):
    make, _ = setup_broker
    blocked = Response(block=True)
    broker, _ = make([blocked, Response()])
    async def scenario():
        operation = (collect(broker) if through_collector else
                     broker._api_get("https://synthetic.invalid/read", "TTTC8001R", {}))
        task = asyncio.create_task(operation)
        await asyncio.wait_for(blocked.entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert kis_rate_limit._state["ledger_busy_since"] == 0
        assert (await collect(broker)).complete
    asyncio.run(scenario())


def test_cancellation_before_acquire_does_not_release_another_ledger_owner(setup_broker):
    make, clock = setup_broker
    broker, session = make([])
    async def scenario():
        await kis_rate_limit.acquire("TTTC8434R")
        owner = kis_rate_limit._state["ledger_busy_since"]
        clock.block = True
        task = asyncio.create_task(broker._api_get("https://synthetic.invalid/read", "TTTC8001R", {}))
        await asyncio.wait_for(clock.waiting.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owner != 0 and kis_rate_limit._state["ledger_busy_since"] == owner
        assert not session.requests
        kis_rate_limit.release_ledger()
    asyncio.run(scenario())


def test_timeout_waiting_for_limiter_preserves_other_owner(setup_broker):
    make, clock = setup_broker
    broker, session = make([], timeout=.01)
    async def scenario():
        await kis_rate_limit.acquire("TTTC8434R")
        owner = kis_rate_limit._state["ledger_busy_since"]
        clock.block = True
        result = await collect(broker)
        assert not result.complete and result.reason == "timeout"
        assert owner != 0 and kis_rate_limit._state["ledger_busy_since"] == owner
        assert not session.requests
        kis_rate_limit.release_ledger()
    asyncio.run(scenario())


def test_cancellation_after_headers_does_not_release_next_inflight_owner(setup_broker):
    make, _ = setup_broker
    parsing = asyncio.Event()

    class BodyBlocked(Response):
        async def json(self):
            parsing.set()
            await asyncio.Event().wait()

    second = Response(block=True)
    broker, _ = make([BodyBlocked(), second])
    async def scenario():
        first_task = asyncio.create_task(collect(broker))
        await asyncio.wait_for(parsing.wait(), timeout=1)
        second_task = asyncio.create_task(collect(broker))
        await asyncio.wait_for(second.entered.wait(), timeout=1)
        owner = kis_rate_limit._state["ledger_busy_since"]
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        assert owner != 0 and kis_rate_limit._state["ledger_busy_since"] == owner
        second_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second_task
        assert kis_rate_limit._state["ledger_busy_since"] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["cancel", "timeout", "late_response"])
def test_stale_takeover_preserves_new_owner_when_previous_get_ends(setup_broker, ending):
    make, clock = setup_broker
    resume = asyncio.Event()

    class Delayed(Response):
        async def __aenter__(self):
            self.entered.set()
            await resume.wait()
            return self

    first = Delayed()
    broker, _ = make([first], timeout=.03 if ending == "timeout" else 15)
    successor = Response(block=True)
    next_broker, _ = make([successor])

    async def scenario():
        first_task = asyncio.create_task(collect(broker))
        second_task = None
        try:
            await asyncio.wait_for(first.entered.wait(), timeout=1)
            assert kis_rate_limit._state["ledger_busy_since"] == 100.0
            clock.value = 110.1
            second_task = asyncio.create_task(collect(next_broker))
            await asyncio.wait_for(successor.entered.wait(), timeout=1)
            assert kis_rate_limit._state["ledger_busy_since"] == 110.1
            if ending == "cancel":
                first_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first_task
            elif ending == "timeout":
                result = await first_task
                assert not result.complete and result.reason == "timeout"
            else:
                resume.set()
                assert (await first_task).complete
            assert kis_rate_limit._state["ledger_busy_since"] == 110.1
            assert kis_rate_limit._state["ledger_last"] == 110.1
        finally:
            first_task.cancel()
            if second_task is not None:
                second_task.cancel()
            await asyncio.gather(first_task, *([second_task] if second_task is not None else []),
                                 return_exceptions=True)
        assert kis_rate_limit._state["ledger_busy_since"] == 0
    asyncio.run(scenario())


def test_ledger_lease_identity_survives_reset_with_same_timestamp(setup_broker):
    _, _clock = setup_broker
    async def scenario():
        previous = await kis_rate_limit.acquire("TTTC8001R")
        kis_rate_limit.reset()
        current = await kis_rate_limit.acquire("TTTC8036R")
        assert previous is not None and current is not None and previous is not current
        kis_rate_limit.release_ledger(previous)
        assert kis_rate_limit._state["ledger_busy_since"] == 100.0
        kis_rate_limit.release_ledger(None)
        assert kis_rate_limit._state["ledger_busy_since"] == 100.0
        kis_rate_limit.release_ledger(current)
        assert kis_rate_limit._state["ledger_busy_since"] == 0
    asyncio.run(scenario())


def test_legacy_unscoped_release_and_stamp_keep_interval_and_invalidate_lease(setup_broker):
    _, clock = setup_broker
    async def scenario():
        previous = await kis_rate_limit.acquire("TTTC8001R")
        kis_rate_limit.release_ledger()
        assert kis_rate_limit._state["ledger_busy_since"] == 0
        current = await kis_rate_limit.acquire("TTTC8036R")
        assert clock.value >= 101.05
        acquired_at = kis_rate_limit._state["ledger_busy_since"]
        kis_rate_limit.release_ledger(previous)
        assert kis_rate_limit._state["ledger_busy_since"] == acquired_at
        kis_rate_limit.stamp_ledger()
        assert kis_rate_limit._state["ledger_busy_since"] == 0
        clock.value += 1
        last = kis_rate_limit._state["ledger_last"]
        kis_rate_limit.release_ledger(current)
        assert kis_rate_limit._state["ledger_last"] == last
    asyncio.run(scenario())


def test_unexpected_http_boundary_failure_releases_ledger_without_retry(setup_broker):
    make, _ = setup_broker
    broker, session = make([Response(error=RuntimeError("PRIVATE")), Response()])
    async def scenario():
        result = await collect(broker)
        assert not result.complete and result.reason == "fetch_failed"
        assert "PRIVATE" not in repr(result)
        assert len(session.requests) == 1 and kis_rate_limit._state["ledger_busy_since"] == 0
        assert (await collect(broker)).complete
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["daily", "cancelable"])
@pytest.mark.parametrize("env", ["dev", "mock"])
def test_unsupported_environment_rejected_before_any_send(setup_broker, kind, env):
    make, _ = setup_broker
    broker, session = make([], env=env)
    with pytest.raises(ValueError, match="production"):
        asyncio.run(collect(broker, kind))
    assert not session.requests and not kis_rate_limit._calls


@pytest.mark.parametrize("status,body", [(401, {}), (500, {"msg_cd": "EGW00123"}),
                                         (200, {"msg_cd": "EGW00121"})])
def test_query_keeps_existing_token_recovery(setup_broker, status, body):
    make, _ = setup_broker
    broker, session = make([Response(status=status, body=body), Response()])
    result = asyncio.run(collect(broker))
    assert result.complete and broker._token_mgr.invalidations == 1
    assert len(session.requests) == 2
    assert session.requests[1]["headers"]["authorization"] == "Bearer SYNTHETIC_REFRESHED_TOKEN"


def test_legacy_dict_get_keeps_continuation_and_no_new_request_header(setup_broker):
    make, _ = setup_broker
    broker, session = make([Response(continuation="F")])
    result = asyncio.run(broker._api_get("https://synthetic.invalid/read", "TTTC8001R", {}))
    assert isinstance(result, dict) and result["rt_cd"] == "0" and result["_tr_cont"] == "F"
    assert "tr_cont" not in session.requests[0]["headers"]
