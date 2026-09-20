"""S3-5: SIGNAL 한 건의 단일 송신로(게시 → prepare → dispatch).

증거는 합성 dict 가 아니라 실제 `CrossStrategyValidator` 와 실제 사이징이 만든 값이고,
owner 는 실제 SQLite + 실제 RegimeOwner 다. 전송은 합성 fake HTTP · 주입 시계 ·
시험용 합성 startup 허가 위의 결과다. **S3 는 설치가 아니다** — 제품의 attach/gateway
호출자는 0건이고 `trading_ready` 는 제품 코드에서 계속 False 다. 실큐 접합은 S3-6a 다.

실행: venv/bin/python -m pytest tests/test_execution_signal_gateway.py -q -p no:cacheprovider
"""
import asyncio
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from loguru import logger

from src.core.types import Order, OrderSide, OrderType
from src.execution.safety.application import ApplicationBlocked
from src.execution.safety.commands import CommandValidationError
from src.execution.safety.gateway import SignalGateway
from src.execution.safety.lifecycle import CommandResult, CommandStatus
from src.execution.safety.runtime import KRExecutionRuntime

from test_cross_validator_characterization import freeze  # noqa: F401 — pytest fixture 재사용
from test_execution_command_owner import fixture as command_fixture
from test_execution_decision_facts import JUSTIFIED, apply_change, economy  # noqa: F401
from test_execution_market_source import market_event
from test_execution_qualification_publishers import SECTOR, buy, evidence_for
from test_execution_regime_recheck import fixture as regime_fixture
from test_execution_risk_policy import NOW
from test_risk_sizing import PRICE, SYM


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


def order(*, symbol=SYM, side=OrderSide.BUY, quantity=10, price=PRICE,
          order_type=OrderType.LIMIT, strategy='sepa_trend'):
    return Order(symbol=symbol, side=side, order_type=order_type, quantity=quantity,
                 price=price if order_type is OrderType.LIMIT else None, strategy=strategy)


async def install(f, monkeypatch, *, config_version=None, exit_manager=None):
    """regime fixture 위에 gateway 를 설치한다. ready 는 이후에도 바꿀 수 있게 목록으로 둔다."""
    f['engine'].broker = f['broker']
    f['ready'] = [True]
    monkeypatch.setattr(KRExecutionRuntime, 'trading_ready',
                        property(lambda self: f['ready'][0]))
    f['quote'] = f.get('quote', f.get('market_quote'))
    gateway = SignalGateway(
        f['runtime'], f['commands'],
        exit_manager=f['exits'] if exit_manager is None else exit_manager,
        config_version=f['ctx'].versions.config if config_version is None else config_version)
    f['runtime'].install_gateway(gateway)
    f['gateway'] = gateway
    return f


async def fixture(tmp_path, monkeypatch, **changes):
    return await install(await regime_fixture(tmp_path, monkeypatch), monkeypatch, **changes)


async def evidence(monkeypatch, freeze_clock, *, symbol=SYM):
    _, found = await evidence_for(monkeypatch, freeze_clock, symbol=symbol)
    return found


def facts_rows(f):
    return f['runtime'].owner.state.get('entry_decision_facts', {})


def only_attempt(f):
    attempts = f['runtime'].owner.state['attempts']
    assert len(attempts) == 1, attempts
    return next(iter(attempts.values()))


def reservations(attempt):
    return (attempt['reserved_quantity'], D(attempt['reserved_cash']),
            D(attempt['reserved_exposure']), attempt['reserved_planned_risk'])


async def paused_submit(f, event, sent, found, *, boundary='connect'):
    """마지막 network await 에서 멈춘 gateway 제출. 이 창에서만 '판단~송신 사이'가 재현된다."""
    reached, release = asyncio.Event(), asyncio.Event()

    async def pause(*args):
        reached.set()
        await release.wait()
        f['broker']._session.closed = False
        return 'synthetic-hash' if boundary == 'hashkey' else True

    if boundary == 'connect':
        f['broker']._session.closed = True
    setattr(f['broker'], {'connect': 'connect', 'hashkey': '_get_hashkey'}[boundary], pause)
    task = asyncio.create_task(f['gateway'].submit(event, sent, found))
    await asyncio.wait_for(reached.wait(), 2)
    return task, release


# ── 생성·설치 경계 (인수 조건 10) ────────────────────────────────────────

@pytest.mark.parametrize('fault', ['exit_manager', 'commands', 'config_version'])
def test_construction_requires_the_parts_that_belong_to_this_runtime(tmp_path, monkeypatch, fault):
    """다른 ExitManager 면 판단~final 의 손절 축이 owner 가 보는 것과 갈라진다."""
    async def scenario():
        from src.strategies.exit_manager import ExitManager
        f = await regime_fixture(tmp_path, monkeypatch)
        try:
            other = await command_fixture(tmp_path / 'other', monkeypatch)
            try:
                parts = dict(exit_manager=f['exits'], config_version=f['ctx'].versions.config)
                commands = f['commands']
                if fault == 'exit_manager':
                    parts['exit_manager'] = ExitManager(persist=False, clock=lambda: NOW)
                elif fault == 'commands':
                    commands = other['commands']
                else:
                    parts['config_version'] = ''
                with pytest.raises(CommandValidationError):
                    SignalGateway(f['runtime'], commands, **parts)
                assert f['runtime'].gateway is None
            finally:
                await other['store'].close()
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['twice', 'foreign_runtime'])
def test_install_gateway_refuses_a_second_writer(tmp_path, monkeypatch, fault):
    """engine 이 safety 패키지를 만나는 길은 하나뿐이다 — 조용히 교체되면 안 된다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            other = await command_fixture(tmp_path / 'other', monkeypatch)
            try:
                installed = f['gateway']
                candidate = (installed if fault == 'twice'
                             else SignalGateway(other['runtime'], other['commands'],
                                                exit_manager=other['exits'],
                                                config_version=other['ctx'].versions.config))
                with pytest.raises(ApplicationBlocked):
                    f['runtime'].install_gateway(candidate)
                assert f['runtime'].gateway is installed
            finally:
                await other['store'].close()
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


# ── 결정 ⑫: 자동 BUY 는 증거 필수, SELL 은 아니다 ───────────────────────

def test_an_automatic_buy_without_evidence_publishes_nothing(tmp_path, monkeypatch, freeze):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            freeze(11, 0, day=18)
            before = f['runtime'].owner.version
            assert await f['gateway'].submit(buy(SYM), order(), None) is None
            state = f['runtime'].owner.state
            assert f['runtime'].owner.version == before
            assert (state['attempts'], facts_rows(f)) == ({}, {})
            assert state.get('entry_quotes', {}) == {}
            assert f['broker']._session.posts == []
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('order_type', [OrderType.LIMIT, OrderType.MARKET])
def test_a_sell_sends_without_evidence_and_without_decision_facts(tmp_path, monkeypatch,
                                                                  freeze, order_type):
    """계약: 판단 사실 소비는 SUBMIT+BUY+AUTOMATIC 뿐이다. 청산을 증거로 막지 않는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            freeze(11, 0, day=18)
            from test_execution_command_owner import held
            await held(f)
            sell = order(side=OrderSide.SELL, order_type=order_type)
            result = await f['gateway'].submit(buy(SYM), sell, None)
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
            assert facts_rows(f) == {}
            assert f['runtime'].owner.state.get('qualification_sources', {}) == {}
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


# ── 정상 경로: 순서·식별자·결과 타입 ─────────────────────────────────────

def test_an_automatic_buy_publishes_prepares_and_posts_once(tmp_path, monkeypatch, freeze):
    """dispatch 를 task 로 감싸면 호출자는 CommandResult 가 아닌 Task 를 받는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            found = await evidence(monkeypatch, freeze)
            result = await f['gateway'].submit(buy(SYM), order(), found)
            assert type(result) is CommandResult
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
            state = f['runtime'].owner.state
            attempt = only_attempt(f)
            assert attempt['command_status'] == 'acknowledged'
            assert attempt['sector'] == SECTOR
            row = facts_rows(f)[attempt['intent_id']]
            assert (row['symbol'], row['strategy']) == (SYM, 'sepa_trend')
            assert row['config_version'] == f['ctx'].versions.config
            assert state['entry_quotes'][SYM]['price'] == str(PRICE)
            assert 'regime' in state['qualification_sources']
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


@pytest.mark.parametrize('skipped, reason', [
    ('quote', 'current_entry_quote_required'),
    ('facts', 'decision_facts_required'),
])
def test_skipping_one_publication_blocks_the_post(tmp_path, monkeypatch, freeze, skipped, reason):
    """게시 순서를 바꾸거나 한 단계를 빼면 owner 가 정확히 이 사유로 막는다(게시 0 이 아니다)."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            found = await evidence(monkeypatch, freeze)

            async def skip(*args, **kwargs):
                return f['runtime'].owner.version

            target = {'quote': 'observe_entry_quote', 'facts': 'publish_decision_facts'}[skipped]
            monkeypatch.setattr(f['commands'], target, skip)
            with pytest.raises(CommandValidationError, match=reason):
                await f['gateway'].submit(buy(SYM), order(), found)
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['broker']._session.posts == []
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


def test_a_bound_market_source_is_not_requoted_by_the_gateway(tmp_path, monkeypatch, freeze):
    """실제 원관측이 이미 진입 가격을 게시했다면 두 번째 진입 시세는 거부된다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            found = await evidence(monkeypatch, freeze)
            event = await market_event(monkeypatch, f['clock'][0], price=str(PRICE))
            await f['runtime'].observe_market(event)
            assert SYM in f['runtime'].owner.state['market_sources']
            calls = []

            original = f['commands'].observe_entry_quote

            async def record(*args, **kwargs):
                calls.append(args)
                return await original(*args, **kwargs)

            monkeypatch.setattr(f['commands'], 'observe_entry_quote', record)
            result = await f['gateway'].submit(buy(SYM), order(), found)
            assert calls == []
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


# ── 결정 ⑫: 부분 게시는 되돌리지 않고, 성공으로도 읽히지 않는다 ─────────

def test_a_failed_facts_publication_is_not_reported_as_success(tmp_path, monkeypatch, freeze):
    """출처만 올라간 부분 상태는 그대로 둔다(version 되감기 금지) — 새 송신 허가는 생기지 않는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            found = await evidence(monkeypatch, freeze)
            original = f['commands'].publish_decision_facts

            async def broken(*args, **kwargs):
                raise ValueError('synthetic-facts-publication-failure')

            monkeypatch.setattr(f['commands'], 'publish_decision_facts', broken)
            with pytest.raises(ValueError, match='synthetic-facts-publication-failure'):
                await f['gateway'].submit(buy(SYM), order(), found)
            published = dict(f['runtime'].owner.state['qualification_sources'])
            assert published['regime']['version'] == 1
            assert facts_rows(f) == {}
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['broker']._session.posts == []

            # 대조: 같은 요청을 다시 제출하면 게시본을 재사용해 정상 진행한다.
            monkeypatch.setattr(f['commands'], 'publish_decision_facts', original)
            result = await f['gateway'].submit(buy(SYM), order(), found)
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert f['runtime'].owner.state['qualification_sources'] == published
            assert len(f['broker']._session.posts) == 1
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


def test_a_regime_citing_buy_without_the_regime_owner_is_refused(tmp_path, monkeypatch, freeze):
    """legacy `_horizons` 폴백은 owner 의 재유도를 통과시키지 못한다(fail-closed)."""
    async def scenario():
        from test_execution_decision_facts import nominal
        f = await install(await command_fixture(tmp_path, monkeypatch, origin='automatic',
                                                policy=nominal()), monkeypatch)
        try:
            assert 'regime_policy' not in f['runtime'].owner.state
            found = await evidence(monkeypatch, freeze)
            with pytest.raises(CommandValidationError, match='stale_regime_decision'):
                await f['gateway'].submit(buy(SYM), order(), found)
            assert f['runtime'].owner.state['attempts'] == {}
            assert f['broker']._session.posts == []
        finally:
            await f['store'].close()
    asyncio.run(scenario())


# ── 판단~송신 사이의 stale 축 (마지막 network await 이후 재검사) ─────────

@pytest.mark.parametrize('change', ['cash', 'reservation', 'daily_loss', 'source',
                                    'config', 'expired'])
def test_a_change_during_the_network_await_blocks_the_post(tmp_path, monkeypatch, freeze, change):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            found = await evidence(monkeypatch, freeze)
            task, release = await paused_submit(f, buy(SYM), order(quantity=JUSTIFIED), found)
            facts = next(iter(facts_rows(f).values()))
            from src.execution.safety.decisions import EntryDecisionFacts
            if change == 'expired':
                # 실제 증거의 만료는 판단 90분 뒤다 — 합성 30분 표본보다 멀리 민다.
                f['clock'][0] = NOW + timedelta(hours=2)
            else:
                await apply_change(f, None, EntryDecisionFacts.from_dict(facts), change)
            release.set()
            result = await task
            assert result.status is CommandStatus.NOT_SENT
            assert f['broker']._session.posts == []
            attempt = f['runtime'].owner.state['attempts'][result.attempt_id]
            assert attempt['state'] == 'final_rejected'
            assert reservations(attempt) == (0, D('0'), D('0'), None)
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


def test_an_unrelated_regime_recommit_during_the_await_still_sends(tmp_path, monkeypatch, freeze):
    """과도 stale 금지: 유효 레짐 문자열이 같으면 새 커밋은 송신을 막지 않는다."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            found = await evidence(monkeypatch, freeze)
            task, release = await paused_submit(f, buy(SYM), order(), found)
            assert (await f['refresh']()).status == 'accepted'
            release.set()
            result = await task
            assert result.status is CommandStatus.ACKNOWLEDGED
            assert len(f['broker']._session.posts) == 1
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


# ── 계약 4·5: 재시도 식별자와 미송신의 예약 ─────────────────────────────

def test_a_retry_reuses_the_intent_with_a_new_attempt_and_leaves_no_reservation(
        tmp_path, monkeypatch, freeze):
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            found = await evidence(monkeypatch, freeze)
            f['ready'][0] = False
            first = await f['gateway'].submit(buy(SYM), order(), found)
            assert (first.status, first.reason_code) == (
                CommandStatus.NOT_SENT, 'startup_reconciliation')
            blocked = f['runtime'].owner.state['attempts'][first.attempt_id]
            assert blocked['state'] == 'final_rejected'
            assert reservations(blocked) == (0, D('0'), D('0'), None)
            assert f['broker']._session.posts == []

            f['ready'][0] = True
            second = await f['gateway'].submit(buy(SYM), order(), found)
            assert second.status is CommandStatus.ACKNOWLEDGED
            assert second.attempt_id != first.attempt_id
            attempts = f['runtime'].owner.state['attempts']
            assert attempts[second.attempt_id]['intent_id'] == blocked['intent_id']
            assert attempts[second.attempt_id]['quantity'] == 10
            assert len(f['broker']._session.posts) == 1
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


def test_the_read_helpers_sum_open_buy_reservations_only(tmp_path, monkeypatch, freeze):
    """engine 의 두 읽기 지점이 owner 의 pending 을 보는 유일한 길이다(이중 정의 금지)."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        gateway = None
        try:
            gateway = f['gateway']
            assert gateway.reserved_cash() == D('0')
            assert gateway.pending_strategy_notional('sepa_trend') == D('0')
            found = await evidence(monkeypatch, freeze)
            result = await f['gateway'].submit(buy(SYM), order(quantity=JUSTIFIED), found)
            assert result.status is CommandStatus.ACKNOWLEDGED
            # ACK 된 BUY 는 아직 미해결 예약이다 — 합에 포함된다.
            assert gateway.reserved_cash() == D('507500.000')
            assert gateway.pending_strategy_notional('sepa_trend') == D('507500.000')
            assert gateway.pending_strategy_notional('gap_and_go') == D('0')

            # 터미널로 끝난 시도는 예약이 없으므로 두 합에서 빠진다.
            def settle(state):
                attempt = state['attempts'][result.attempt_id]
                attempt.update(state='final_rejected', reserved_cash='0', reserved_quantity=0,
                               reserved_exposure='0', reserved_planned_risk=None)
                return state
            await f['runtime'].owner.mutate('synthetic-terminal', settle)
            assert gateway.reserved_cash() == D('0')
            assert gateway.pending_strategy_notional('sepa_trend') == D('0')
        finally:
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())


# ── 결정 ⑩·⑬: 게시 로그가 유일한 감사 흔적이고 금액을 싣지 않는다 ───────

def test_the_facts_publication_leaves_one_audit_line_without_money(tmp_path, monkeypatch, freeze):
    """S3-4 가 전일 facts 를 지우므로 이 로그가 유일한 흔적이다(금액·계좌 금지)."""
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        lines = []
        sink = logger.add(lambda message: lines.append(str(message)), level='INFO')
        try:
            found = await evidence(monkeypatch, freeze)
            result = await f['gateway'].submit(buy(SYM), order(quantity=JUSTIFIED), found)
            assert result.status is CommandStatus.ACKNOWLEDGED
            tagged = [line for line in lines if '[게이트웨이]' in line]
            assert len(tagged) == 1
            attempt = only_attempt(f)
            row = facts_rows(f)[attempt['intent_id']]
            for value in (attempt['intent_id'], SYM, 'sepa_trend', row['digest'],
                          row['config_version'], row['expires_at']):
                assert value in tagged[0]
            for secret in ('507500', 'test-scope', str(JUSTIFIED * 10000)):
                assert secret not in tagged[0]
        finally:
            logger.remove(sink)
            await f['runtime'].shutdown(); await f['store'].close()
    asyncio.run(scenario())
