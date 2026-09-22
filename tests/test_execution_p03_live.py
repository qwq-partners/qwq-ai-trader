"""P0-3 S-B/S-C/S-D: live 파일 3개(`kis_kr`·`kr_scheduler`·`batch_analyzer`)의 인수.

이 단계가 지키는 계약은 하나다 — **attach 미설치(`engine._execution_runtime is None`)에서는
legacy 동작이 0줄 바뀌지 않는다.** 새 분기는 전부 그 가드 안이고, 가드 밖 변경은 기본값이
종래와 같은 가법 kwarg 2개와 분류 함수 위임뿐이다.

여기서 GREEN 인 것은 배선의 계약뿐이다. 실 KIS 응답의 철자·거래소 범위·취소 최종성은
계획서 §5 스모크 4항이 닫기 전에는 아무것도 증명되지 않았다.
"""
import asyncio
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from test_kis_execution_query_integration import NOW, Response, setup_broker  # noqa: F401

# ---------------------------------------------------------------- S-B ① 기본값 불변

# 종래(`904c982`) 의 `get_execution_daily` 가 보내던 일별조회 params 전문. 가법 kwarg 가
# 기본값으로 불려도 바이트 단위로 이것과 같아야 한다.
DAILY_PARAMS_BEFORE = {
    "CANO": "SYNTHETIC_ACCOUNT", "ACNT_PRDT_CD": "SYNTHETIC_PRODUCT",
    "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
    "INQR_STRT_DT": "20260918", "INQR_END_DT": "20260918",
    "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "01", "PDNO": "", "CCLD_DVSN": "00",
    "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
    "EXCG_ID_DVSN_CD": "KRX",
}


def test_the_default_call_sends_exactly_what_it_sent_before(setup_broker):
    """기본 인자 호출의 요청 params 와 페이지 예산이 종래와 같다."""
    make, _ = setup_broker
    broker, session = make([Response(rows=[{"odno": "one"}])])
    result = asyncio.run(broker.get_execution_daily(
        account_scope="test-scope", start_date="2026-09-18", end_date="2026-09-18",
        clock=lambda: NOW))
    assert result.complete and result.scope.exchange_scope == "KRX"
    assert session.requests[0]["params"] == DAILY_PARAMS_BEFORE
    assert broker._execution_queries(lambda: NOW)._max_pages == 10


def test_the_scope_and_the_page_budget_reach_the_collector(setup_broker):
    """`exchange_scope`·`max_pages` 가 파서가 아니라 수집기까지 내려간다."""
    make, _ = setup_broker
    # 헤더가 계속 'F' 라 상한이 걸릴 때까지 돈다. max_pages 가 전달되지 않으면 기본 10 이다.
    pages = [Response(continuation="F", rows=[{"odno": str(i)}], cursor=(f"F{i}", f"N{i}"))
             for i in range(10)]
    broker, session = make(pages)
    result = asyncio.run(broker.get_execution_daily(
        account_scope="test-scope", start_date="2026-09-18", end_date="2026-09-18",
        clock=lambda: NOW, exchange_scope="ALL", max_pages=3))
    assert len(result.pages) == 3 and not result.complete and result.reason == "page_limit"
    assert len(session.requests) == 3
    assert result.scope.exchange_scope == "ALL"
    assert {request["params"]["EXCG_ID_DVSN_CD"] for request in session.requests} == {"ALL"}


# ---------------------------------------------------------------- S-B ② 분류 위임


def test_the_scheduler_method_delegates_to_the_single_source(monkeypatch):
    """스케줄러 메서드는 사본이 아니라 `utils.exit_types` 의 위임이다.

    20개 입력 동일성은 S-A 의 `test_execution_p03_wiring` 이 이미 고정한다. 여기서 막는
    것은 본문이 다시 복사돼 두 사본이 갈라지는 것이다.
    """
    from src.schedulers import kr_scheduler
    monkeypatch.setattr(kr_scheduler, "classify_exit_type", lambda reason: f"위임:{reason}")
    assert kr_scheduler.KRScheduler._classify_exit_type("손절 -5.2%") == "위임:손절 -5.2%"


# ---------------------------------------------------------------- S-C sync 읽기 전용


@pytest.fixture
def attached_sync(monkeypatch, tmp_path):
    """미설치 특성화 시험의 가짜 봇을 그대로 쓰고 engine 에 runtime 만 심는다.

    RiskManager 는 진짜다 — 인수가 말하는 것은 기록용 목록이 아니라 `_sync_healthy` 다.
    """
    from pathlib import Path

    from src.core.types import RiskConfig
    from src.risk.manager import RiskManager
    from src.utils import loop_heartbeat as hb
    import test_sync_portfolio_characterization as legacy

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hb._states.pop("kr_portfolio_sync", None)

    def make(*, owner_positions, balance, kis_positions, cash="100000", attached=True):
        sched, bot, _ = legacy._make(monkeypatch, bot_positions=owner_positions,
                                     balance=balance, kis_seq=[kis_positions] * 20, cash=cash)
        bot.risk_manager = RiskManager(RiskConfig(), D(cash), "KR")
        bot.engine._execution_runtime = object() if attached else None
        return sched, bot

    yield make, legacy, hb
    hb._states.pop("kr_portfolio_sync", None)


def _owner_view(bot):
    """`_owner_ready` 가 쓰는 바로 그 부호화 — 여기가 바뀌면 게이트가 닫힌다."""
    from src.execution.safety.economics import encode_portfolio
    return encode_portfolio(bot.engine.portfolio)


def test_a_quiet_account_leaves_the_owner_view_untouched_for_ten_cycles(attached_sync):
    """조용한 계좌: 10회 sync 후 owner 게시 포트폴리오·ExitManager 가 그대로다.

    수량·현금은 같지만 owner 의 `current_price`(수락된 시세)와 KIS 의 `prpr` 은 다르다 —
    legacy 꼬리는 그 한 줄을 덮어 `_owner_ready` 의 DTO 동등성을 깨고, 그래서 조용한
    계좌에서도 30초마다 게이트가 닫힌다(계획서 §1). attach 분기는 그 줄에 닿지 않는다.
    """
    make, legacy, hb = attached_sync
    sched, bot = make(owner_positions=[legacy._pos("005930", qty=10, cur="10000")],
                      balance={"available_cash": 100000, "stock_value": 105000},
                      kis_positions={"005930": legacy._pos("005930", qty=10, cur="10500")})
    before = _owner_view(bot)
    for _ in range(10):
        legacy._run(sched)
    assert _owner_view(bot) == before
    assert (bot.exit_manager.registered, bot.exit_manager.removed) == ([], [])
    assert bot.risk_manager._sync_healthy is True
    assert hb._state("kr_portfolio_sync").consecutive_failures == 0
    # 조회는 계속 하지만 쓰기는 0 — 재시도 방어를 거치지 않은 단순 경로다.
    assert bot.broker.get_positions_calls == 10


def test_a_manual_sell_is_reported_and_never_written_into_the_owner_view(attached_sync):
    """수동 매도로 KIS 수량 < owner 수량 → 실패 기록 + error 1건, live 는 그대로."""
    make, legacy, hb = attached_sync
    sched, bot = make(owner_positions=[legacy._pos("005930", qty=10)],
                      balance={"available_cash": 100000, "stock_value": 42000},
                      kis_positions={"005930": legacy._pos("005930", qty=4)})
    before = _owner_view(bot)
    errors = []
    from src.schedulers import kr_scheduler
    handle = kr_scheduler.logger.add(lambda message: errors.append(message), level="ERROR")
    try:
        legacy._run(sched)
    finally:
        kr_scheduler.logger.remove(handle)
    assert _owner_view(bot) == before
    assert bot.risk_manager._sync_fail_count == 1
    assert hb._state("kr_portfolio_sync").consecutive_failures == 1
    assert len(errors) == 1 and "005930 수량 owner 10 ≠ KIS 4" in errors[0]
    # 계좌번호가 로그로 새지 않는다.
    assert "SYNTHETIC_ACCOUNT" not in errors[0]


def test_the_symmetric_difference_and_the_cash_gap_are_reported(attached_sync):
    """종목 대칭차·현금 차(1,000원 임계) 세 갈래를 한 번에 고정한다."""
    make, legacy, _ = attached_sync
    sched, bot = make(owner_positions=[legacy._pos("005930", qty=10)],
                      balance={"available_cash": 100500, "stock_value": 105000},
                      kis_positions={"000660": legacy._pos("000660", qty=3)})
    reasons = sched._attached_sync_mismatches(bot.broker.balance, {"000660": legacy._pos("000660")})
    assert reasons == ["owner 에만 있음: 005930", "KIS 에만 있음: 000660"]
    # 현금 차는 1,000원 임계 위에서만 센다.
    assert sched._attached_sync_mismatches({"available_cash": 100999}, {}) == [
        "owner 에만 있음: 005930"]
    assert sched._attached_sync_mismatches({"available_cash": 98000}, {}) == [
        "owner 에만 있음: 005930", "현금 차 2,000원"]
    # available_cash 가 0 이하이면 legacy 와 같이 '정보 없음' — 불일치로 올리지 않는다.
    assert sched._attached_sync_mismatches({"available_cash": 0}, {}) == [
        "owner 에만 있음: 005930"]


def test_a_broken_comparison_is_not_charged_to_the_kis_sync(attached_sync, monkeypatch):
    """대조 자체가 터져도 fail-closed 이고, 사유가 KIS 동기화 실패와 구분된다."""
    make, legacy, hb = attached_sync
    sched, bot = make(owner_positions=[], balance={"available_cash": 100000, "stock_value": 0},
                      kis_positions={})

    def boom(*args, **kwargs):
        raise RuntimeError("대조 결함")

    monkeypatch.setattr(type(sched), "_attached_sync_mismatches", boom)
    legacy._run(sched)
    assert bot.risk_manager._sync_fail_count == 1
    assert hb._state("kr_portfolio_sync").failure_reason == "attach 대조 실패: 대조 결함"
