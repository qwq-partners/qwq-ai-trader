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
