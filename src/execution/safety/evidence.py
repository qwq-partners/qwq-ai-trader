"""고정 KIS 국내 주문 관측의 보수적인 순수 파서.

TTTC0081R 전체조회에서 단순 KRX 정규장 현금 주문의 전량체결만 종결로 지원한다.
취소·정정 체인·다른 TR/세션은 미지원이며 API 호출·재전송은 수행하지 않는다.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from .lifecycle import OrderEvidence, OrderRef, OrderState, valid_evidence_provenance


OFFICIAL_REVISION = "b4e6249714418aa57833d1cbbbced39cbcc5b125"
OFFICIAL_CONTRACT_HASHES = {
    "inquire_daily_ccld.py": "9a039eef4f4c61e6d3b5ec15a85b18508e69f5b069bbbf447a0ba5cfe6044b5b",
    "chk_inquire_daily_ccld.py": "e97ad745cac44abeff7615c68afe96258dc664da1ed60db33d49b4736a34735c",
    "inquire_psbl_rvsecncl.py": "c51f69737ebf2faf5bda92030e81deffe43f7f30d1214afc409f3a5481f36fd0",
}
CONTRACT_ID = f"kis-domestic-daily-ccld:{OFFICIAL_REVISION}:TTTC0081R"


@dataclass(frozen=True)
class EvidencePage:
    rows: list[dict]
    continuation: str | None
    next_cursor: tuple[str, str] = ("", "")
    request_cursor: tuple[str, str] = ("", "")
    request_cont: str = ""
    success: bool = True


def _int(value):
    if type(value) is int and value >= 0:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ValueError("invalid integer")
    return int(value)


def _str(value):
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("invalid string")
    return value


def _parse_row(row):
    if not isinstance(row, dict):
        raise ValueError("invalid row")
    normalized = {}
    for key, value in row.items():
        if not isinstance(key, str):
            raise ValueError("invalid field name")
        lowered = key.lower()
        if lowered in normalized and normalized[lowered] != value:
            raise ValueError("conflicting field aliases")
        normalized[lowered] = value
    row = normalized
    day = _str(row["ord_dt"])
    if not re.fullmatch(r"[0-9]{8}", day):
        raise ValueError("invalid date")
    day = datetime.strptime(day, "%Y%m%d").date().isoformat()
    side_code = _str(row["sll_buy_dvsn_cd"])
    if side_code not in ("01", "02"):
        raise ValueError("invalid side")
    cancel = _str(row["cncl_yn"])
    if cancel not in ("Y", "N"):
        raise ValueError("invalid cancel flag")
    # 취소확인수량 철자: 공식 저장소는 cnc_cfrm_qty(chk_inquire_daily_ccld.py:41) 한 철자뿐이고
    # 포털 가이드는 cncl_cfrm_qty다. 어느 쪽이 정본인지 저장소가 판정하지 않으므로(Q24) 두 표기를
    # 모두 받아들이되, 둘 다 오면 값이 같을 때만 수용하고 다르면 malformed로 거부한다. 둘 다
    # 없으면 지금처럼 거부한다 — 결측을 0으로 채우지 않는다.
    cancel_fields = [field for field in ("cnc_cfrm_qty", "cncl_cfrm_qty") if field in row]
    if not cancel_fields:
        raise KeyError("cnc_cfrm_qty")
    cancel_values = [_int(row[field]) for field in cancel_fields]
    if cancel_values[0] != cancel_values[-1]:
        raise ValueError("conflicting cancel confirmation quantities")
    numbers = [_int(row[field]) for field in ("ord_qty", "tot_ccld_qty", "rmn_qty")]
    numbers.extend((cancel_values[0], _int(row["rjct_qty"])))
    qty, filled, remaining, cancelled, rejected = numbers
    if qty <= 0 or any(q > qty for q in numbers[1:]) or filled + cancelled + rejected + remaining > qty:
        raise ValueError("impossible quantities")
    amount_raw = row["tot_ccld_amt"]
    if isinstance(amount_raw, bool) or not isinstance(amount_raw, (str, int, Decimal)):
        raise ValueError("invalid amount")
    amount = Decimal(str(amount_raw))
    if not amount.is_finite() or amount < 0 or (filled == 0 and amount != 0) or (filled > 0 and amount <= 0):
        raise ValueError("invalid amount")
    strings = {field: _str(row[field]) for field in (
        "odno", "orgn_odno", "ord_gno_brno", "pdno", "excg_id_dvsn_cd", "ord_dvsn_cd")}
    if not strings["odno"] or not strings["pdno"] or not strings["excg_id_dvsn_cd"]:
        raise ValueError("missing identity")
    return dict(strings, day=day, side="sell" if side_code == "01" else "buy", qty=qty,
                filled=filled, amount=amount, remaining=remaining, cancelled=cancelled,
                rejected=rejected, cancel=cancel)


def _complete(pages, max_pages):
    """연속조회 종료 판정 — F/M은 다음 페이지, D/E는 마지막, 그 밖은 완결 아님.

    공식 저장소가 같은 규칙을 명시한다: legacy/Sample01/kis_domstk.py:275,278
    `if tr_cont == "D" or tr_cont == "E": # 마지막 페이지 … elif tr_cont == "F" or tr_cont == "M":`
    (요청 tr_cont는 2페이지부터 "N" — inquire_daily_ccld.py:193-204). 본문 커서는 마지막
    페이지에도 채워져 오므로 종료 근거가 못 된다.
    """
    if not pages or len(pages) > max_pages:
        return False
    expected_cursor = ("", "")
    seen = set()
    for index, page in enumerate(pages):
        if (not isinstance(page, EvidencePage) or not page.success or not isinstance(page.rows, list)
                or page.request_cursor != expected_cursor or page.request_cont != ("N" if index else "")):
            return False
        if page.continuation in ("D", "E"):
            return index == len(pages) - 1
        if page.continuation not in ("F", "M"):
            return False
        cursor = page.next_cursor
        if (not isinstance(cursor, tuple) or len(cursor) != 2
                or any(not isinstance(v, str) for v in cursor) or not any(cursor) or cursor in seen):
            return False
        seen.add(cursor)
        expected_cursor = cursor
    return False


def parse_order_evidence(ref: OrderRef, symbol: str, side: str, pages: list[EvidencePage], *,
                         tr_id: str = "TTTC0081R", session: str = "regular",
                         query_kind: str = "all", max_pages: int = 10,
                         observed_at: datetime | None = None, request_started_at: datetime | None = None,
                         query_scope: dict | None = None, now: datetime | None = None) -> OrderEvidence:
    """빈 조회·불완전 조회를 취소로 추정하지 않고 응답 행을 정규화한다.

    호출자는 실제 요청 계좌/시장과 query_scope를 연결한다. 날짜·거래소·지점·원주문
    식별자는 응답에서도 일치해야 한다. TTTC8001R에 신형 계약을 상속하지 않는다.

    supported는 아래 조건 전부의 AND다. 그중 cncl_yn의 Y/N 강제와 ord_dvsn_cd
    제한("00"/"01")은 보수적 선택이다 — 공식 저장소는 이 두 필드의 값 집합도, 정상
    주문에서 어떤 값이 오는지도 말하지 않는다(Q25·라벨뿐인
    chk_inquire_daily_ccld.py:22-57). 따라서 그 밖의 값은 전량체결이어도
    unsupported_finality로 남긴다. 좁히는 방향의 오판만 허용한다.

    chain 판정은 조회한 거래소 범위에 의존한다 — 수집기는 KRX만 조회하므로 NXT/SOR에
    있는 자식행은 보이지 않는다(운영 경로의 조회는 ALL이다). 범위를 좁히는 것이 항상
    fail-closed는 아니다.
    """
    complete = _complete(pages, max_pages)
    query_scope = dict(query_scope or {})
    source_contract = f"kis-domestic-daily-ccld:{OFFICIAL_REVISION}:{tr_id}"
    unknown = dict(ref=ref, symbol=symbol, side=side, complete=complete,
                   source_contract=source_contract, reason="not_found", observed_at=observed_at,
                   request_started_at=request_started_at, query_scope=query_scope)
    provenance = valid_evidence_provenance(ref, observed_at, request_started_at, query_scope,
                                            now if now is not None else datetime.now(timezone.utc))
    provenance = provenance and (query_scope["tr_id"], query_scope["session"], query_scope["query_kind"]) == (tr_id, session, query_kind)
    if not provenance:
        return OrderEvidence(**dict(unknown, schema_valid=False, reason="invalid_provenance"))
    try:
        rows = [_parse_row(row) for page in pages if isinstance(page, EvidencePage)
                and page.success for row in page.rows]
    except (ValueError, KeyError, TypeError, InvalidOperation):
        return OrderEvidence(**dict(unknown, schema_valid=False, reason="malformed_row"))
    matches = [r for r in rows if (r["day"], r["odno"], r["ord_gno_brno"], r["orgn_odno"], r["excg_id_dvsn_cd"])
               == (ref.order_date, ref.order_no, ref.org_no, ref.parent_order_no, ref.exchange)]
    if not matches:
        return OrderEvidence(**unknown)
    value = matches[0]
    if any(r != value for r in matches[1:]):
        return OrderEvidence(**dict(unknown, schema_valid=False, reason="conflicting_rows"))
    if (value["pdno"], value["side"]) != (symbol, side):
        return OrderEvidence(**dict(unknown, schema_valid=False, reason="identity_conflict"))
    chain = bool(ref.parent_order_no) or any(r["orgn_odno"] == ref.order_no for r in rows)
    supported = (complete and ref.market == "KR" and ref.exchange == "KRX" and not chain
                 and tr_id == "TTTC0081R" and session == "regular" and query_kind == "all"
                 and value["ord_dvsn_cd"] in ("00", "01") and value["cancel"] == "N"
                 and value["filled"] == value["qty"] and value["remaining"] == 0
                 and value["cancelled"] == 0 and value["rejected"] == 0)
    return OrderEvidence(ref, symbol, side, value["qty"], value["filled"], value["amount"],
                         value["remaining"], value["cancelled"],
                         OrderState.FINAL_FILLED if supported else OrderState.RECONCILING,
                         complete=complete, supported_finality=supported, source_contract=source_contract,
                         reason="" if supported else ("incomplete" if not complete else "unsupported_finality"),
                         rejected_quantity=value["rejected"], observed_at=observed_at,
                         request_started_at=request_started_at, query_scope=query_scope)
