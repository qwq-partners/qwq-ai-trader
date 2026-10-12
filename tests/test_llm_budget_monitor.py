import asyncio
from types import SimpleNamespace

import pytest


def _payload(**overrides):
    payload = {
        "kind": "transition",
        "event_at": "2026-10-12T23:10:00+09:00",
        "month": "2026-10",
        "spent_usd": 79.25,
        "reserved_usd": 1.0,
        "monthly_limit_usd": 100.0,
        "policy_mode": "economy",
        "policy_reason": "threshold_80",
        "policy_version": "budget-economy-v1",
    }
    payload.update(overrides)
    return payload


class FakeLedger:
    def __init__(
        self, notice=None, *, status=None, status_error=None,
        claim_error=None, finish_error=None
    ):
        self.notice = notice
        self.status = status
        self.status_error = status_error
        self.claim_error = claim_error
        self.finish_error = finish_error
        self.events = []

    def policy_status(self):
        self.events.append(("status",))
        if self.status_error:
            raise self.status_error
        return self.status or _payload()

    def claim_policy_notice(self, lease_seconds=120):
        self.events.append(("claim", lease_seconds))
        if self.claim_error:
            raise self.claim_error
        notice, self.notice = self.notice, None
        return notice

    def finish_policy_notice(self, event_id, claim_token, delivered):
        self.events.append(("finish", event_id, claim_token, delivered))
        if self.finish_error:
            raise self.finish_error


class FakeManager:
    def __init__(self, ledger, *, enabled=True):
        self.monthly_budget = ledger
        self.config = SimpleNamespace(budget_policy_enabled=enabled)


def _notice(payload=None):
    return {
        "event_id": "evt-1",
        "claim_token": "claim-1",
        "payload": payload or _payload(),
    }


def test_formatter_reports_cost_reservations_thresholds_and_trading_boundary():
    from src.utils.llm_budget_monitor import format_budget_policy_notice

    text = format_budget_policy_notice(_payload(), event_id="evt-visible")

    assert "LLM 월 예산 정책 전환" in text
    assert "2026-10 (KST)" in text
    assert "2026-10-12T23:10:00+09:00" in text
    assert (
        "정산 추정 $79.2500 + 예약 추정 $1.0000 = 관리 합계 $80.2500 "
        "/ 월 한도 $100.0000 (80.2%)"
    ) in text
    assert "이벤트 ID: evt-visible" in text
    assert "절약 모드 (80% 이상)" in text
    assert "공개 단가·토큰 사용량 추정이며 공급자 청구서 확정액이 아닙니다" in text
    assert "절약 80% → 필수 95% → 유료 중지 100%" in text
    assert "위험 중요 판단 모델은 자동 하향하지 않습니다" in text
    assert "기존 매매 규칙·매수 중지 설정과 보호 청산은 유지됩니다" in text


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("normal", "기본 모드 (80% 미만)"),
        ("essential", "필수 호출 모드 (95% 이상)"),
        ("paused", "유료 호출 중지 (100% 이상)"),
        ("blocked", "안전 차단 (비용 미확인 또는 원장 이상)"),
    ],
)
def test_formatter_names_every_policy_mode(mode, expected):
    from src.utils.llm_budget_monitor import format_budget_policy_notice

    assert expected in format_budget_policy_notice(_payload(policy_mode=mode))


def test_month_reset_reports_previous_month_and_does_not_clear_unresolved_usage():
    from src.utils.llm_budget_monitor import format_budget_policy_notice

    text = format_budget_policy_notice(_payload(
        kind="month_reset",
        month="2026-11",
        event_at="2026-11-01T00:00:01+09:00",
        spent_usd=0.0,
        reserved_usd=0.0,
        policy_mode="normal",
        policy_reason="kst_month_reset",
        previous_month={
            "month": "2026-10",
            "spent_usd": 94.0,
            "reserved_usd": 0.5,
            "unresolved_count": 2,
        },
    ))

    assert "KST 달력월 전환" in text
    assert (
        "이전 월 2026-10 (월 경계 시점 잠정값): "
        "정산 추정 $94.0000 + 예약 추정 $0.5000"
    ) in text
    assert "미확인 전체 건수: 2건" in text
    assert "새 월 2026-11" in text
    assert "미확인 비용과 원장 이상은 월 전환으로 초기화되지 않습니다" in text


def test_blocked_month_reset_does_not_claim_normal_policy_restored():
    from src.utils.llm_budget_monitor import format_budget_policy_notice

    text = format_budget_policy_notice(_payload(
        kind="month_reset",
        month="2026-11",
        policy_mode="blocked",
        policy_reason="unresolved_usage",
        previous_month={
            "month": "2026-10",
            "spent_usd": 94.0,
            "reserved_usd": 0.0,
            "unresolved_count": 1,
        },
    ))

    assert "차단 상태는 유지됩니다" in text
    assert "새 월 예산으로 전환합니다" not in text


@pytest.mark.asyncio
async def test_tick_observes_policy_claims_one_notice_and_acks_success():
    from src.utils.llm_budget_monitor import budget_monitor_tick

    ledger = FakeLedger(_notice())
    sent = []

    async def send(text):
        ledger.events.append(("send",))
        sent.append(text)
        return True

    assert await budget_monitor_tick(FakeManager(ledger), send=send) is True
    assert len(sent) == 1
    assert "이벤트 ID: evt-1" in sent[0]
    assert ledger.events == [
        ("status",),
        ("claim", 120),
        ("send",),
        ("finish", "evt-1", "claim-1", True),
    ]


@pytest.mark.asyncio
async def test_tick_distinguishes_queued_event_policy_from_delivery_policy():
    from src.utils.llm_budget_monitor import budget_monitor_tick

    ledger = FakeLedger(
        _notice(_payload(month="2026-10", policy_mode="economy")),
        status=_payload(month="2026-11", policy_mode="normal"),
    )
    sent = []

    async def send(text):
        sent.append(text)
        return True

    assert await budget_monitor_tick(FakeManager(ledger), send=send) is True
    assert "기록 시점 정책: <b>절약 모드 (80% 이상)</b>" in sent[0]
    assert "발송 시점 정책: <b>기본 모드 (80% 미만)</b> (2026-11 KST)" in sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("private transport detail")])
async def test_tick_keeps_failed_notice_retryable(failure):
    from src.utils.llm_budget_monitor import budget_monitor_tick

    ledger = FakeLedger(_notice())

    async def send(_text):
        if failure is False:
            return False
        raise failure

    assert await budget_monitor_tick(FakeManager(ledger), send=send) is False
    assert ledger.events[-1] == ("finish", "evt-1", "claim-1", False)


@pytest.mark.asyncio
async def test_tick_propagates_cancellation_without_finishing_claim():
    from src.utils.llm_budget_monitor import budget_monitor_tick

    ledger = FakeLedger(_notice())

    async def cancel(_text):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await budget_monitor_tick(FakeManager(ledger), send=cancel)
    assert not any(event[0] == "finish" for event in ledger.events)


@pytest.mark.asyncio
async def test_tick_times_out_delivery_before_lease_and_marks_failure(monkeypatch):
    import src.utils.llm_budget_monitor as monitor

    ledger = FakeLedger(_notice())
    observed = []

    async def send(_text):
        return True

    async def timeout(awaitable, timeout):
        observed.append(timeout)
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(monitor.asyncio, "wait_for", timeout)
    assert await monitor.budget_monitor_tick(FakeManager(ledger), send=send) is False
    assert observed == [60]
    assert ledger.events[-1] == ("finish", "evt-1", "claim-1", False)


@pytest.mark.asyncio
async def test_disabled_or_missing_ledger_is_silent():
    from src.utils.llm_budget_monitor import budget_monitor_tick

    disabled = FakeLedger(_notice())
    assert await budget_monitor_tick(FakeManager(disabled, enabled=False), send=None) is None
    assert disabled.events == []
    assert await budget_monitor_tick(FakeManager(None), send=None) is None


@pytest.mark.asyncio
async def test_storage_error_is_generic_rate_limited_and_does_not_crash(monkeypatch):
    import src.utils.llm_budget_monitor as monitor

    monitor._last_error_log.clear()
    secret = "secret-ledger-path-and-detail"
    ledger = FakeLedger(status_error=RuntimeError(secret))
    messages = []
    monkeypatch.setattr(monitor.logger, "warning", messages.append)

    assert await monitor.budget_monitor_tick(FakeManager(ledger), send=None) is None
    assert await monitor.budget_monitor_tick(FakeManager(ledger), send=None) is None
    assert len(messages) == 1
    assert "예산 정책 상태 확인 실패" in messages[0]
    assert secret not in messages[0]


@pytest.mark.asyncio
async def test_monitor_repeats_ticks_and_propagates_loop_cancellation(monkeypatch):
    import src.utils.llm_budget_monitor as monitor

    ledger = FakeLedger()
    sleeps = []

    async def cancel_sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(monitor.asyncio, "sleep", cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await monitor.run_budget_monitor(
            FakeManager(ledger), send=lambda _text: None, interval_seconds=17
        )
    assert ledger.events == [("status",), ("claim", 120)]
    assert sleeps == [17]


@pytest.mark.asyncio
async def test_monitor_recovers_after_unexpected_tick_error(monkeypatch):
    import src.utils.llm_budget_monitor as monitor
    calls = []
    async def tick(*a, **kw):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError('synthetic')
        raise asyncio.CancelledError
    async def sleep(_):
        return None
    monkeypatch.setattr(monitor, 'budget_monitor_tick', tick)
    monkeypatch.setattr(monitor.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        await monitor.run_budget_monitor(FakeManager(FakeLedger()))
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_missing_telegram_adapter_keeps_notice_retryable(monkeypatch):
    from src.utils import telegram
    from src.utils.llm_budget_monitor import budget_monitor_tick
    monkeypatch.delattr(telegram, 'send_alert')
    ledger = FakeLedger(_notice())
    assert await budget_monitor_tick(FakeManager(ledger)) is False
    assert ledger.events[-1] == ('finish', 'evt-1', 'claim-1', False)


@pytest.mark.asyncio
@pytest.mark.parametrize('configured', [True, False])
async def test_real_telegram_wrapper_boolean_and_html_contract(monkeypatch, configured):
    from src.utils import telegram
    from src.utils.llm_budget_monitor import budget_monitor_tick
    requests = []
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
    def post(url, *, json, timeout):
        requests.append(json)
        return Response()
    notifier = object.__new__(telegram.TelegramNotifier)
    notifier.bot_token = 'synthetic' if configured else ''
    notifier.alert_chat_id = 'synthetic'
    notifier._session = SimpleNamespace(closed=False, post=post)
    monkeypatch.setattr(telegram, 'get_telegram_notifier', lambda: notifier)
    ledger = FakeLedger(_notice())
    assert await budget_monitor_tick(FakeManager(ledger)) is configured
    assert ledger.events[-1] == ('finish', 'evt-1', 'claim-1', configured)
    if configured:
        assert requests[0]['parse_mode'] == 'HTML'
        assert '<b>' in requests[0]['text']
    else:
        assert not requests
