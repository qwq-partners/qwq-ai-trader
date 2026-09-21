"""공식 저장소(koreainvestment/open-trading-api@b4e6249) 기준 정합만 고정한다.

저장소는 요청·응답의 모양을 고정해 줄 뿐 취소·체결의 최종성을 보장하지 않는다.
여기서 고정하는 것은 철자 수용·TR 값·거래소 범위 명시뿐이고, 최종성 인정 범위는
넓히지 않는다(근거: docs/integrations/kis-repo-grounding-2026-09-21.md).
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.execution.safety.evidence import EvidencePage, parse_order_evidence
from src.execution.safety.lifecycle import OrderRef
from src.execution.safety.queries import LegacyExecutionQueries, QueryResponse
from src.utils import kis_rate_limit


REF = OrderRef("account-1", "KR", "2026-09-17", "KRX", "000123", "branch")
NOW = datetime(2026, 9, 17, 3, tzinfo=timezone.utc)
SCOPE = {"account_scope": "account-1", "market": "KR", "exchange": "KRX",
         "start_date": "2026-09-17", "end_date": "2026-09-17", "tr_id": "TTTC0081R",
         "query_kind": "all", "session": "regular"}


_ABSENT = object()  # 그 키를 아예 보내지 않는 응답을 뜻한다


def row(**changes):
    value = {"ord_dt": "20260917", "odno": "000123", "orgn_odno": "", "ord_gno_brno": "branch",
             "pdno": "005930", "sll_buy_dvsn_cd": "01", "ord_qty": "10", "tot_ccld_qty": "10",
             "tot_ccld_amt": "1000", "rmn_qty": "0", "cnc_cfrm_qty": "0", "rjct_qty": "0",
             "cncl_yn": "N", "excg_id_dvsn_cd": "KRX", "ord_dvsn_cd": "00"}
    value.update(changes)
    return {key: item for key, item in value.items() if item is not _ABSENT}


def parse(pages, **kwargs):
    options = {"observed_at": NOW, "request_started_at": NOW - timedelta(seconds=1), "now": NOW,
               "query_scope": SCOPE}
    options.update(kwargs)
    return parse_order_evidence(REF, "005930", "sell", pages, **options)


# --- (1) 취소확인수량 두 철자 -------------------------------------------------

@pytest.mark.parametrize("spelling", ["cnc_cfrm_qty", "cncl_cfrm_qty"])
def test_either_cancel_confirm_spelling_is_accepted_alone(spelling):
    fields = {"cnc_cfrm_qty": _ABSENT, spelling: "0"}
    evidence = parse([EvidencePage([row(**fields)], "D")])
    assert evidence.schema_valid and evidence.supported_finality
    assert evidence.cancelled_quantity == 0


def test_both_cancel_confirm_spellings_agree_and_preserve_the_value():
    evidence = parse([EvidencePage([row(tot_ccld_qty="5", tot_ccld_amt="500", cncl_yn="Y",
                                        cnc_cfrm_qty="5", cncl_cfrm_qty="5")], "D")])
    assert evidence.schema_valid and evidence.cancelled_quantity == 5
    assert not evidence.supported_finality and evidence.reason == "unsupported_finality"


def test_disagreeing_cancel_confirm_spellings_are_malformed_not_reconciled():
    evidence = parse([EvidencePage([row(cnc_cfrm_qty="0", cncl_cfrm_qty="1")], "D")])
    assert not evidence.schema_valid and evidence.reason == "malformed_row"
    assert not evidence.supported_finality


def test_missing_both_cancel_confirm_spellings_is_still_refused():
    evidence = parse([EvidencePage([row(cnc_cfrm_qty=_ABSENT)], "D")])
    assert not evidence.schema_valid and evidence.reason == "malformed_row"


# --- (2)(3) 수집기 TR·거래소 범위 --------------------------------------------

def response(rows=None, *, continuation="D", cursor=("", ""), kind="daily"):
    return QueryResponse(200, {"tr_cont": continuation}, {
        "rt_cd": "0", "output1" if kind == "daily" else "output": rows or [],
        "ctx_area_fk100": cursor[0], "ctx_area_nk100": cursor[1],
    })


def collector_for(pages):
    requests = []

    async def fetch(request):
        requests.append(request)
        return pages[len(requests) - 1]

    return LegacyExecutionQueries(fetch, clock=lambda: NOW, request_timeout=1), requests


def collect_daily(pages, **kwargs):
    collector, requests = collector_for(pages)
    args = dict(account_scope="account-1", account_number="SYNTHETIC_ACCOUNT",
                product_code="SYNTHETIC_PRODUCT", start_date="2026-09-17", end_date="2026-09-17")
    args.update(kwargs)
    return asyncio.run(collector.daily(**args)), requests


def test_daily_uses_the_repository_tr_and_sends_the_krx_scope_explicitly():
    result, requests = collect_daily([response([row()])])
    assert requests[0].tr_id == "TTTC0081R"
    assert requests[0].params["EXCG_ID_DVSN_CD"] == "KRX"
    assert result.scope.tr_id == "TTTC0081R" and result.scope.exchange_scope == "KRX"


def test_this_scope_mapping_passes_provenance_and_the_legacy_tr_is_refused_by_finality():
    """수집기 scope를 이렇게 매핑해 파서에 넘기면 provenance를 통과한다는 것만 잰다.

    통과는 최종성 인정이 아니다 — 수집기 query_kind는 "daily"라 supported의
    query_kind == "all" 조건에서 이미 걸린다. 뒷부분은 scope와 인자를 **함께**
    TTTC8001R로 맞춘 경우다: 범위 불일치가 사라지므로 provenance는 통과하고, 거절은
    invalid_provenance가 아니라 unsupported_finality로 나타난다.
    """
    result, _ = collect_daily([response([row()])])
    scope = result.scope
    collected = {"account_scope": scope.account_scope, "market": scope.market,
                 "exchange": scope.exchange_scope, "start_date": scope.start_date,
                 "end_date": scope.end_date, "tr_id": scope.tr_id,
                 "query_kind": scope.query_kind, "session": "regular"}
    evidence = parse([EvidencePage([row()], "D")], query_scope=collected,
                     tr_id=scope.tr_id, query_kind=scope.query_kind)
    assert evidence.reason != "invalid_provenance" and evidence.cumulative_quantity == 10
    legacy = parse([EvidencePage([row()], "D")], query_scope=dict(collected, tr_id="TTTC8001R"),
                   tr_id="TTTC8001R", query_kind=scope.query_kind)
    assert legacy.reason == "unsupported_finality" and not legacy.supported_finality
    assert legacy.source_contract.endswith(":TTTC8001R")


def test_cancelable_uses_the_repository_tr_without_an_exchange_parameter():
    collector, requests = collector_for([response(kind="cancelable")])
    result = asyncio.run(collector.cancelable(account_scope="account-1", account_number="A",
                                              product_code="P"))
    assert requests[0].tr_id == "TTTC0084R"
    assert "EXCG_ID_DVSN_CD" not in requests[0].params
    assert result.scope.exchange_scope != "KRX"
    assert result.complete and result.finality_supported is False


def test_truncated_or_empty_cancelable_collection_never_supports_finality():
    collector, _ = collector_for([response(kind="cancelable")])
    empty = asyncio.run(collector.cancelable(account_scope="account-1", account_number="A",
                                             product_code="P"))
    assert empty.complete and not empty.rows and empty.finality_supported is False
    truncated_pages = [response([{"odno": str(index)}], continuation="M", cursor=("F", str(index)),
                                kind="cancelable") for index in range(2)]
    collector, _ = collector_for(truncated_pages)
    truncated = asyncio.run(collector.cancelable(account_scope="account-1", account_number="A",
                                                 product_code="P"))
    assert not truncated.complete and truncated.finality_supported is False


# --- (5) 보수적 최종성 조건 ---------------------------------------------------

@pytest.mark.parametrize("ord_dvsn_cd", ["05", "41", "27"])
def test_other_order_divisions_stay_unsupported_even_when_fully_filled(ord_dvsn_cd):
    evidence = parse([EvidencePage([row(ord_dvsn_cd=ord_dvsn_cd)], "D")])
    assert evidence.schema_valid and evidence.cumulative_quantity == 10
    assert not evidence.supported_finality and evidence.reason == "unsupported_finality"


def test_cancel_flagged_row_stays_unsupported_even_when_fully_filled():
    """cncl_yn='Y'인 전량체결 행은 schema_valid이지만 최종성을 인정하지 않는다.

    수량은 모두 정상이고(체결 10/10, 잔량·취소확인·거부 0) 취소 표시만 'Y'다. 공식
    저장소는 이 필드의 값 집합도 전량체결 행에 무엇이 오는지도 말하지 않으므로(Q25)
    수량만 보고 종결로 올리지 않는다 — supported의 cncl_yn == 'N' 조건을 하중한다.
    """
    evidence = parse([EvidencePage([row(cncl_yn="Y")], "D")])
    assert evidence.schema_valid and evidence.cumulative_quantity == 10
    assert evidence.cancelled_quantity == 0 and evidence.rejected_quantity == 0
    assert not evidence.supported_finality and evidence.reason == "unsupported_finality"


@pytest.mark.parametrize("changes", [{"rmn_qty": "1"}, {"cnc_cfrm_qty": "1"}, {"rjct_qty": "1"}])
def test_quantity_conservation_is_what_makes_the_three_zero_conditions_redundant(changes):
    """supported의 remaining/cancelled/rejected == 0 은 단독으로 하중할 수 없다.

    파서가 filled+cancelled+rejected+remaining > qty 를 malformed로 거부하고 모든 수량이
    음수가 아니므로, filled == qty 인 순간 나머지 셋은 반드시 0이다. 따라서 그 세 조건을
    각각 지우는 변이를 죽이는 표본은 존재할 수 없다(보존식 때문에 만들 수 없는 조합).
    대신 세 조건을 잉여로 만들어 주는 보존식 자체를 여기서 고정한다 — 이 식이 풀리면
    전량체결과 잔량/취소/거부가 동시에 살아 있는 행이 통과하게 된다.
    """
    evidence = parse([EvidencePage([row(**changes)], "D")])
    assert not evidence.schema_valid and evidence.reason == "malformed_row"
    assert not evidence.supported_finality


# --- (6) 신·구 원장 TR 유량 --------------------------------------------------

@pytest.mark.parametrize("tr_id", ["TTTC0081R", "TTTC0084R", "TTTC8001R", "TTTC8036R"])
def test_new_and_legacy_inquiry_trs_are_both_serialized_as_ledger_calls(tr_id):
    assert tr_id in kis_rate_limit.LEDGER_TR_IDS and kis_rate_limit.is_ledger(tr_id)
    try:
        lease = asyncio.run(kis_rate_limit.acquire(tr_id))
        assert lease is not None
        assert kis_rate_limit._state["ledger_busy_since"] > 0
    finally:
        kis_rate_limit.reset()
