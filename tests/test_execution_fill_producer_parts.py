"""P0-2 1단계 부품: 2단계 파싱·chain 노출·거래소 대조·수집기 어댑터·관측·binding.

제품 호출자는 0건이다. 합성 페이지는 실 KIS 취소/체결 최종성의 증명이 아니며, 여기서
GREEN인 것은 부품의 계약뿐이다(설치 승인이 아니다).
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from src.core.types import Order, OrderSide
from src.execution.safety.application import FillObservation, observation_from_evidence
from src.execution.safety.commands import CommandValidationError
from src.execution.safety.evidence import EvidencePage, evidence_pages, parse_order_evidence, parser_scope
from src.execution.safety.gateway import SignalGateway
from src.execution.safety.initial_r import _validate_finality
from src.execution.safety.lifecycle import OrderEvidence, OrderRef, OrderState
from src.execution.safety.transport import GuardedKISTransport
from test_kis_execution_queries import collect_pages, daily, response
from test_kis_order_evidence import NOW, REF, SCOPE, parse, row


ALL_SCOPE = dict(SCOPE, exchange="ALL")


def unrelated(**changes):
    """계좌 안의 무관한 주문 한 행(우리 odno가 아니다)."""
    return row(odno="000999", **changes)


# ---------------------------------------------------------------- 2단계 파싱

def test_unrelated_strictly_broken_row_no_longer_erases_our_observation():
    other = unrelated()
    del other["cnc_cfrm_qty"]  # 무관한 주문의 필드 결측
    evidence = parse([EvidencePage([row(), other], "D")])
    assert evidence.schema_valid and evidence.supported_finality
    assert evidence.cumulative_quantity == 10 and evidence.chain is False


def test_unreadable_identity_row_is_treated_like_a_chain_not_ignored():
    evidence = parse([EvidencePage([row(), unrelated(orgn_odno=None)], "D")])
    assert evidence.schema_valid and evidence.cumulative_quantity == 10
    assert evidence.chain is True and not evidence.supported_finality
    assert evidence.reason == "chain_undecidable"


def test_conflicting_identity_alias_makes_the_row_unreadable_not_arbitrary():
    evidence = parse([EvidencePage([row(), unrelated(ODNO="000123")], "D")])
    assert evidence.chain is True and not evidence.supported_finality


def test_our_row_is_still_parsed_strictly():
    evidence = parse([EvidencePage([row(tot_ccld_qty="-1"), unrelated()], "D")])
    assert not evidence.schema_valid and evidence.reason == "malformed_row"


def test_missing_order_with_unreadable_rows_is_not_reported_as_not_found():
    evidence = parse([EvidencePage([unrelated(orgn_odno=None)], "D")])
    assert not evidence.schema_valid and evidence.reason == "chain_undecidable"
    assert evidence.chain is True


def test_child_chain_row_is_exposed_while_quantity_stays_as_before():
    evidence = parse([EvidencePage(
        [row(), row(odno="000124", orgn_odno="000123", tot_ccld_qty="0", tot_ccld_amt="0")], "D")])
    assert evidence.chain is True and evidence.cumulative_quantity == 10
    assert not evidence.supported_finality and evidence.reason == "unsupported_finality"


# ---------------------------------------------------------------- 거래소 대조

def test_our_order_filled_on_another_exchange_is_named_not_silently_missing():
    evidence = parse([EvidencePage([row(excg_id_dvsn_cd="NXT")], "D")], query_scope=ALL_SCOPE)
    assert not evidence.schema_valid and evidence.reason == "exchange_mismatch"
    assert evidence.cumulative_quantity == 0 and not evidence.supported_finality


def test_a_matching_non_krx_row_is_read_but_never_proven_final():
    """행의 거래소가 ref와 같아도 KRX가 아니면 종결을 인정하지 않는다(대조 가드와 별개).

    대조 가드(`excg_id_dvsn_cd != ref.exchange`)만 남기면 이 행이 통과한다 — supported의
    `ref.exchange == "KRX"` 는 그 가드가 덮지 않는 조건이고, 여기 없는 거래소의 취소
    최종성은 공식 자료에도 없다.
    """
    ref = OrderRef("account-1", "KR", "2026-09-17", "NXT", "000123", "branch")
    evidence = parse_order_evidence(
        ref, "005930", "sell", [EvidencePage([row(excg_id_dvsn_cd="NXT")], "D")],
        observed_at=NOW, request_started_at=NOW - timedelta(seconds=1), now=NOW,
        query_scope=ALL_SCOPE)
    assert evidence.schema_valid and evidence.cumulative_quantity == 10
    assert not evidence.supported_finality and evidence.reason == "unsupported_finality"
    assert evidence.state is OrderState.RECONCILING


def test_all_scope_query_still_matches_and_finalizes_a_krx_row():
    evidence = parse([EvidencePage([row()], "D")], query_scope=ALL_SCOPE)
    assert evidence.supported_finality and evidence.state is OrderState.FINAL_FILLED


def test_initial_r_finality_consumer_accepts_the_same_all_scope_provenance():
    evidence = parse([EvidencePage([row()], "D")], query_scope=ALL_SCOPE)
    attempt = {"kind": "submit", "order_ref": REF.to_dict(), "symbol": "005930", "side": "sell",
               "quantity": 10, "observed_quantity": 10, "observed_amount": "1000"}
    _validate_finality(attempt, evidence, NOW)
    narrowed = OrderEvidence(**dict(
        {field: getattr(evidence, field) for field in evidence.__dataclass_fields__},
        query_scope=dict(SCOPE, exchange="NXT")))
    with pytest.raises(ValueError):
        _validate_finality(attempt, narrowed, NOW)


# ---------------------------------------------------------------- 수집기 어댑터

def collection(rows_first, rows_second, **kwargs):
    collector, requests = collect_pages([response(rows_first, continuation="F", cursor=("a", "b")),
                                         response(rows_second, continuation="D", cursor=("a", "b"))])
    return asyncio.run(daily(collector, account_scope=REF.account_scope, **kwargs)), requests


def test_real_collector_pages_feed_the_parser_and_prove_one_full_fill():
    result, requests = collection([unrelated()], [row()], exchange_scope="ALL")
    assert requests[0].params["EXCG_ID_DVSN_CD"] == "ALL" and result.scope.exchange_scope == "ALL"
    scope = parser_scope(result.scope, session="regular")
    assert scope["query_kind"] == "all" and scope["exchange"] == "ALL"
    assert scope["start_date"] == "2026-09-17" and scope["tr_id"] == "TTTC0081R"
    evidence = parse_order_evidence(REF, "005930", "sell", evidence_pages(result),
                                    tr_id=result.scope.tr_id, session="regular",
                                    observed_at=result.completed_at,
                                    request_started_at=result.started_at,
                                    query_scope=scope, now=result.completed_at)
    assert evidence.complete and evidence.supported_finality
    assert evidence.cumulative_quantity == 10 and evidence.cumulative_amount == D("1000")


def test_frozen_collector_rows_without_thawing_are_not_evidence():
    result, _ = collection([], [row()])
    raw = [EvidencePage(list(page.rows), page.response_cont,
                        page.next_cursor if page.next_cursor is not None else ("", ""),
                        page.request_cursor, page.request_cont)
           for page in result.pages]
    evidence = parse_order_evidence(REF, "005930", "sell", raw, observed_at=result.completed_at,
                                    request_started_at=result.started_at,
                                    query_scope=parser_scope(result.scope, session="regular"),
                                    now=result.completed_at)
    assert not evidence.schema_valid


def test_truncated_collection_cannot_become_complete_through_the_adapter():
    collector, _ = collect_pages([response([row()], continuation="F", cursor=("a", "b")),
                                  TimeoutError("network")])
    result = asyncio.run(daily(collector))
    assert not result.complete and len(result.pages) == 1
    evidence = parse_order_evidence(REF, "005930", "sell", evidence_pages(result),
                                    observed_at=result.completed_at,
                                    request_started_at=result.started_at,
                                    query_scope=parser_scope(result.scope, session="regular"),
                                    now=result.completed_at)
    assert not evidence.complete and not evidence.supported_finality


def test_unsupported_exchange_scope_is_refused_before_any_request():
    collector, requests = collect_pages([response([row()])])
    with pytest.raises(ValueError):
        asyncio.run(daily(collector, exchange_scope="NXT"))
    assert requests == []


# ---------------------------------------------------------------- 관측 재구성

METADATA = {"entry_signal_score": 80.0, "sector": "반도체"}


def observation(evidence, metadata=METADATA):
    return observation_from_evidence(evidence, trading_day=REF.order_date, metadata=metadata)


def test_observation_from_evidence_is_deterministic_and_fee_free():
    evidence = parse([EvidencePage([row()], "D")])
    first, again = observation(evidence), observation(evidence)
    assert first.order_key == REF.key and first.cumulative_fee == D("0")
    assert first.side == "SELL" and first.cumulative_quantity == 10
    assert first.observation_id == again.observation_id


def test_stored_state_reconstruction_yields_the_same_observation_id():
    parsed = observation(parse([EvidencePage([row()], "D")]))
    # 저장된 attempt 행(order_ref/symbol/side/observed_quantity/observed_amount)만으로 되살린다.
    stored = OrderEvidence(OrderRef.from_dict(REF.to_dict()), "005930", "sell", 10, 10, D("1000"))
    assert observation(stored).observation_id == parsed.observation_id
    assert observation(stored).to_dict() == parsed.to_dict()


def test_metadata_is_part_of_the_observation_identity():
    evidence = parse([EvidencePage([row()], "D")])
    assert observation(evidence).observation_id != observation(evidence, {}).observation_id
    assert observation(evidence, {}).metadata == {}


# ---------------------------------------------------------------- binding·게이트웨이

def test_gateway_binds_the_signal_score_and_refuses_an_unusable_one():
    buy = Order(symbol="005930", side=OrderSide.BUY, quantity=10, price=D("10000"))
    sell = Order(symbol="005930", side=OrderSide.SELL, quantity=10, price=D("10000"))
    event = type("E", (), {"score": 80.0})()
    assert SignalGateway._fill_metadata(event, buy) == {"entry_signal_score": 80.0}
    assert SignalGateway._fill_metadata(event, sell) is None
    # 0점은 유효한 주문 intent다 — falsy 폴백으로 바꾸면 신호 점수 80이 새어 들어온다.
    buy.signal_score = 0.0
    assert SignalGateway._fill_metadata(event, buy) == {"entry_signal_score": 0.0}
    buy.signal_score = D("80")
    with pytest.raises(CommandValidationError):
        SignalGateway._fill_metadata(event, buy)


@pytest.mark.parametrize("metadata", [
    {"stop_loss_pct": 5}, {"entry_signal_score": D("80")}, {"entry_signal_score": True},
    {"entry_signal_score": float("nan")}, {"sector": " 반도체"}, {"name": ""}, {"exit_type": 3},
])
def test_prepare_refuses_unusable_fill_metadata_before_any_reservation(tmp_path, monkeypatch, metadata):
    from test_execution_command_owner import fixture
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            request = f["request"]()
            await f["quote"](request)
            before = f["runtime"].owner.version
            with pytest.raises(CommandValidationError):
                await f["commands"].prepare(request, f["entry"](request), fill_metadata=metadata)
            assert f["runtime"].owner.version == before
            assert "A" not in f["runtime"].owner.state.get("attempts", {})
        finally: await f["store"].close()
    asyncio.run(scenario())


def test_binding_carries_the_session_and_metadata_and_reconcile_accepts_all_scope(tmp_path, monkeypatch):
    from test_execution_command_owner import fixture
    async def scenario():
        f = await fixture(tmp_path, monkeypatch)
        try:
            request = f["request"]()
            await f["quote"](request)
            attempt = await f["commands"].prepare(
                request, f["entry"](request), sector="반도체",
                fill_metadata={"entry_signal_score": 80, "exit_type": "stop_loss"})
            binding = attempt["request_binding"]
            assert binding["session"] == request.session.session == "regular"
            # 정수 점수는 float으로 정규화한다(Decimal은 encode_state를 통과하지 못한다).
            assert binding["fill_metadata"] == {"entry_signal_score": 80.0, "exit_type": "stop_loss"}
            # 80 == 80.0 이라 동등 비교만으로는 정규화가 사라져도 통과한다.
            assert type(binding["fill_metadata"]["entry_signal_score"]) is float
            result = await f["commands"].dispatch(request, f["entry"](request),
                GuardedKISTransport(f["broker"], request_builder=f["builder"]))
            ref, now = result.order_ref, f["clock"][0]
            scope = dict(account_scope=ref.account_scope, market="KR", exchange="ALL",
                         start_date=ref.order_date, end_date=ref.order_date,
                         tr_id="TTTC0081R", query_kind="all", session=binding["session"])
            evidence = OrderEvidence(ref, request.symbol, "buy", request.quantity, 4,
                                     D("40000"), 6, 0, OrderState.PARTIAL, complete=True,
                                     source_contract="synthetic-parts-only", observed_at=now,
                                     request_started_at=now, query_scope=scope)
            assert await f["runtime"].lifecycle.reconcile(request.attempt_id, evidence)
            stored = f["runtime"].owner.state["attempts"][request.attempt_id]
            assert stored["observed_quantity"] == 4 and stored["query_scope"]["exchange"] == "ALL"
        finally: await f["store"].close()
    asyncio.run(scenario())
