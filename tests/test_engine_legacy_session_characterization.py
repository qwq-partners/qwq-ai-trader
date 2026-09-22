"""P0-4 S-C(C5): legacy 브로커의 세션 경계 특성화 — attach 와 나란히 두는 **대조군**.

`tests/test_engine_legacy_stale_eviction_characterization.py` 의 자매 파일이다. 여기서
고정하는 것은 "현행 결함 그대로"가 아니라 **격차의 방향**이다:

- legacy 는 세션을 **POST 직전에** 읽어 본문을 만든다(`kis_kr.py:508`·`:533`·`:556-559`)
  — 판단 시점과 송신 시점이 어긋날 창 자체가 없다. 그래서 15:21(`closing`)의 지정가
  보호 SELL 은 **그대로 나간다**.
- attach 는 prepare 시점에 세션을 본문·fingerprint·증거 라벨에 굽는다 — 15:19 에 구운
  요청을 15:21 에 보내면 `request_session_changed` 로 **사라진다**
  (`tests/test_execution_dispatch_reasons.py` 의 C1).
- 두 구현이 **같이** 막는 구간도 있다: `break`(15:30~15:40)·`closed`(20:00~)과
  동시호가의 시장가. legacy 는 거절 뒤 매 시세 틱의 재감지로 의도를 살리지만
  (`engine.py:2588-2594` 의 SELL 무쿨다운) attach 에는 그 루프가 없다(차단 사유 21).

외부 I/O 0건 — 합성 session/token/hashkey/limiter 위에서 돈다. 제품 코드는 바꾸지 않는다.

실행: venv/bin/python -m pytest tests/test_engine_legacy_session_characterization.py -q -p no:cacheprovider
"""
import asyncio
from datetime import datetime as _RealDateTime
from decimal import Decimal as D
from pathlib import Path

import pytest

import src.execution.broker.kis_kr as kis_kr
from src.core.types import Order, OrderSide, OrderType

from test_kr_prepared_dispatch import fixture as broker_fixture


@pytest.fixture(autouse=True)
def synthetic_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)


SYMBOL = '005930'
# attach 의 `_session_at` 과 같은 경계를 legacy 가 쓰는지까지 이 파일이 확인한다.
MOMENTS = {'regular': (15, 19), 'closing': (15, 21), 'break': (15, 31),
           'next_market': (15, 41), 'closed': (20, 1)}


def legacy_broker(monkeypatch, hour, minute):
    """실제 `KISBroker.submit_order` 를 합성 전송 경계 위에서 돌린다."""
    broker, _, _ = broker_fixture()
    broker._pending_orders = {}
    broker._order_id_to_kis_no = {}
    broker._order_id_to_orgno = {}
    broker._token_mgr._is_token_valid = lambda: True

    async def limiter(tr=None):
        # legacy `_api_post` 는 tr 없이 부른다(owner transport 와 호출 모양이 다르다).
        return None

    broker._rate_limit = limiter
    stamp = _RealDateTime(2026, 9, 18, hour, minute)

    class Frozen(_RealDateTime):
        @classmethod
        def now(cls, tz=None):
            return stamp if tz is None else stamp.replace(tzinfo=tz)

    monkeypatch.setattr(kis_kr, 'datetime', Frozen)
    return broker


def protection_sell(order_type=OrderType.LIMIT):
    return Order(symbol=SYMBOL, side=OrderSide.SELL, quantity=60, price=D('10000'),
                 order_type=order_type, strategy='sepa_trend', reason='stop_loss')


def test_the_legacy_session_table_uses_the_same_boundaries_as_the_owner(monkeypatch):
    """경계가 어긋나면 "legacy 는 보내고 attach 만 죽는다"는 대조 자체가 성립하지 않는다."""
    from src.execution.safety.requests import _session_at
    from zoneinfo import ZoneInfo

    for name, (hour, minute) in MOMENTS.items():
        broker = legacy_broker(monkeypatch, hour, minute)
        assert broker._get_current_market_session() == name
        moment = _RealDateTime(2026, 9, 18, hour, minute, tzinfo=ZoneInfo('Asia/Seoul'))
        assert _session_at(moment) == name


def test_legacy_accepts_a_limit_protection_sell_in_the_closing_auction(tmp_path, monkeypatch):
    """15:21 — attach 가 `request_session_changed` 로 죽는 바로 그 순간에 legacy 는 POST 한다."""
    async def scenario():
        broker = legacy_broker(monkeypatch, *MOMENTS['closing'])
        accepted, detail = await broker.submit_order(protection_sell())
        assert accepted is True
        assert len(broker._session.posts) == 1
        _, kwargs = broker._session.posts[0]
        # 동시호가는 지정가 본문(`ORD_DVSN='00'`)이고 시간외 표시가 붙지 않는다.
        assert kwargs['json']['ORD_DVSN'] == '00'
        assert 'AFHR_FLPR_YN' not in kwargs['json']
        assert kwargs['json']['SLL_TYPE'] == '01'
        assert detail == '1234567890' or detail.startswith('TEMP_')
    asyncio.run(scenario())


@pytest.mark.parametrize('session, order_type, fragment', [
    ('closing', OrderType.MARKET, '동시호가'),
    ('break', OrderType.LIMIT, '휴장'),
    ('closed', OrderType.LIMIT, '장 마감'),
])
def test_legacy_refuses_the_same_sessions_the_owner_cannot_build(tmp_path, monkeypatch,
                                                                 session, order_type, fragment):
    """두 구현이 같이 막는 구간 — 거부의 **모양**만 다르다(legacy 는 반환값, attach 는 예외).

    legacy 는 여기서 POST 를 내지 않고 반환으로 끝나며, 청산 의도는 다음 시세 틱의
    재감지가 되살린다. attach 는 요청 객체를 만들지 못해 ErrorEvent 로 끝난다.
    """
    async def scenario():
        broker = legacy_broker(monkeypatch, *MOMENTS[session])
        accepted, detail = await broker.submit_order(protection_sell(order_type))
        assert accepted is False
        assert fragment in detail
        assert broker._session.posts == []
    asyncio.run(scenario())
