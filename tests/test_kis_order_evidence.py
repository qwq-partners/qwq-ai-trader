"""고정 KIS 예제로 관측을 정규화하되 취소 최종성을 추정하지 않는 시험."""
from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pytest

from src.execution.safety.lifecycle import OrderRef, OrderState
from src.execution.safety.evidence import EvidencePage, parse_order_evidence

REF = OrderRef("account-1", "KR", "2026-09-17", "KRX", "000123", "branch")
NOW = datetime(2026, 9, 17, 3, tzinfo=timezone.utc)
SCOPE = {"account_scope": "account-1", "market": "KR", "exchange": "KRX",
         "start_date": "2026-09-17", "end_date": "2026-09-17", "tr_id": "TTTC0081R",
         "query_kind": "all", "session": "regular"}


def row(**changes):
    value = {"ord_dt": "20260917", "odno": "000123", "orgn_odno": "", "ord_gno_brno": "branch",
             "pdno": "005930", "sll_buy_dvsn_cd": "01", "ord_qty": "10", "tot_ccld_qty": "10",
             "tot_ccld_amt": "1000", "rmn_qty": "0", "cnc_cfrm_qty": "0", "rjct_qty": "0",
             "cncl_yn": "N", "excg_id_dvsn_cd": "KRX", "ord_dvsn_cd": "00"}
    value.update(changes)
    return value


def parse(pages, **kwargs):
    options = {"observed_at": NOW, "request_started_at": NOW-timedelta(seconds=1), "now": NOW,
               "query_scope": SCOPE}
    options.update(kwargs)
    return parse_order_evidence(REF, "005930", "sell", pages, **options)


def test_simple_exact_full_fill_is_supported_but_duplicate_rows_count_once():
    evidence = parse([EvidencePage([row(), row()], "D")])
    assert evidence.complete and evidence.supported_finality
    assert evidence.state is OrderState.FINAL_FILLED
    assert evidence.cumulative_quantity == 10
    assert evidence.cumulative_amount == Decimal("1000")


def test_cancel_quantity_fields_do_not_invent_proven_finality():
    evidence = parse([EvidencePage([row(tot_ccld_qty="5", tot_ccld_amt="500", cnc_cfrm_qty="5", cncl_yn="Y")], "D")])
    assert evidence.cumulative_quantity == 5
    assert not evidence.supported_finality
    assert evidence.reason == "unsupported_finality"


@pytest.mark.parametrize("pages", [
    [EvidencePage([row()], "M", next_cursor=("a", "b"))],
    [EvidencePage([row()], "M", next_cursor=("a", "b")), EvidencePage([], "D", success=False, request_cursor=("a", "b"), request_cont="N")],
    [EvidencePage([row()], "M", next_cursor=("a", "b")), EvidencePage([], "M", next_cursor=("a", "b"), request_cursor=("a", "b"), request_cont="N")],
    [EvidencePage([row()], None)],
    [EvidencePage([row()], "M", next_cursor=("a", "b")), EvidencePage([], "D")],
])
def test_incomplete_pagination_cannot_prove_finality(pages):
    evidence = parse(pages)
    assert not evidence.complete
    assert not evidence.supported_finality


def test_full_pagination_requires_next_request_n_and_matching_cursor():
    pages = [EvidencePage([], "M", next_cursor=("a", "b")),
             EvidencePage([row()], "D", request_cursor=("a", "b"), request_cont="N")]
    assert parse(pages).supported_finality


@pytest.mark.parametrize("changes", [
    {"tot_ccld_qty": "-1"}, {"tot_ccld_qty": "10.0"}, {"tot_ccld_qty": True},
    {"tot_ccld_amt": "NaN"}, {"tot_ccld_amt": "Infinity"}, {"tot_ccld_amt": "-1"},
    {"ord_dt": "20260230"}, {"sll_buy_dvsn_cd": "garbage"},
    {"rmn_qty": None}, {"tot_ccld_qty": "11"}, {"cncl_yn": "?"},
])
def test_malformed_or_impossible_rows_are_not_complete_evidence(changes):
    evidence = parse([EvidencePage([row(**changes)], "D")])
    assert not evidence.supported_finality
    assert not evidence.schema_valid


def test_conflicting_duplicate_order_rows_are_not_deduplicated_away():
    evidence = parse([EvidencePage([row(), row(tot_ccld_amt="999")], "D")])
    assert evidence.reason == "conflicting_rows"
    assert not evidence.supported_finality


def test_cancel_child_chain_and_other_scope_do_not_add_to_original_fills():
    evidence = parse([EvidencePage([row(), row(odno="000124", orgn_odno="000123", tot_ccld_qty="0", tot_ccld_amt="0")], "D")])
    assert not evidence.supported_finality
    assert evidence.cumulative_quantity == 10
    assert evidence.reason == "unsupported_finality"


@pytest.mark.parametrize("kwargs", [{"tr_id": "TTTC8001R"}, {"session": "pre_market"}, {"query_kind": "fills"}])
def test_unverified_query_contract_does_not_inherit_supported_finality(kwargs):
    assert not parse([EvidencePage([row()], "D")], **kwargs).supported_finality


def test_empty_complete_query_does_not_prove_missing_order_cancelled():
    evidence = parse([EvidencePage([], "D")])
    assert evidence.complete
    assert evidence.state is OrderState.BLOCKED_UNKNOWN
    assert not evidence.supported_finality


def test_missing_required_status_field_is_unknown_not_zero():
    truncated = row()
    del truncated["cnc_cfrm_qty"]
    assert not parse([EvidencePage([truncated], "D")]).schema_valid


def test_pagination_cap_is_incomplete_even_if_rows_include_target():
    assert not parse([EvidencePage([row()], "M", next_cursor=("a", "b"))], max_pages=1).complete


def test_conflicting_case_aliases_are_not_silently_overwritten():
    assert not parse([EvidencePage([row(TOT_CCLD_QTY="9")], "D")]).schema_valid


def test_failed_page_with_malformed_rows_remains_unknown_without_exception():
    evidence = parse([EvidencePage(None, "D")])
    assert not evidence.complete and not evidence.schema_valid


@pytest.mark.parametrize("changes", [
    {"observed_at": None}, {"observed_at": NOW.replace(tzinfo=None)},
    {"observed_at": NOW+timedelta(seconds=1)},
    {"query_scope": dict(SCOPE, account_scope="other")},
    {"query_scope": dict(SCOPE, start_date="2026-09-16", end_date="2026-09-16")},
])
def test_missing_wrong_scope_or_future_provenance_never_proves_finality(changes):
    evidence = parse([EvidencePage([row()], "D")], **changes)
    assert not evidence.supported_finality


def test_observation_preserves_aware_reception_and_requested_query_scope():
    evidence = parse([EvidencePage([row()], "D")])
    assert evidence.observed_at == NOW
    assert evidence.request_started_at == NOW-timedelta(seconds=1)
    assert evidence.query_scope == SCOPE


def test_future_order_day_is_unknown_even_with_matching_query_scope():
    before = datetime(2026, 9, 16, 14, 59, tzinfo=timezone.utc)
    evidence = parse([EvidencePage([row()], "D")], now=before, observed_at=before,
                     request_started_at=before-timedelta(seconds=1))
    assert not evidence.supported_finality
    assert evidence.reason == "invalid_provenance"


def test_kst_day_boundary_uses_observation_zone_not_host_utc_day():
    after = datetime(2026, 9, 16, 15, 1, tzinfo=timezone.utc)  # KST 09/17 00:01
    evidence = parse([EvidencePage([row()], "D")], now=after, observed_at=after,
                     request_started_at=after-timedelta(seconds=1))
    assert evidence.supported_finality


def test_unsupported_legacy_tr_keeps_actual_source_label():
    evidence = parse([EvidencePage([row()], "D")], tr_id="TTTC8001R",
                     query_scope=dict(SCOPE, tr_id="TTTC8001R"))
    assert not evidence.supported_finality
    assert evidence.source_contract.endswith(":TTTC8001R")
    assert "TTTC0081R" not in evidence.source_contract
