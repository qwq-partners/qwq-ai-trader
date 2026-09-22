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


IDENTITY_FIELDS = ("ord_dt", "odno", "ord_gno_brno", "orgn_odno")


def _identity(row):
    """모든 행에서 신원 4필드만 방어적으로 읽는다. 읽을 수 없으면 None(unreadable_row).

    후보와 chain 판정은 계좌 전체 행을 봐야 하지만, 무관한 주문의 필드 결측이 우리 주문의
    관측을 지워서는 안 된다(15필드 엄격 파싱은 후보 행에만 적용한다). 소문자 정규화와 별칭
    충돌 검사는 `_parse_row`와 같게 하되 **신원 4필드에 한정**한다 — 무관한 필드의 별칭
    충돌은 우리 행이면 뒤의 엄격 파싱이 잡고, 신원 필드의 충돌은 임의 값을 고르지 않는다.
    """
    if not isinstance(row, dict):
        return None
    picked = {}
    for key, value in row.items():
        if not isinstance(key, str):
            return None
        lowered = key.lower()
        if lowered not in IDENTITY_FIELDS:
            continue
        if lowered in picked and picked[lowered] != value:
            return None
        picked[lowered] = value
    if picked.keys() != set(IDENTITY_FIELDS):
        return None
    for value in picked.values():
        if not isinstance(value, str) or value != value.strip():
            return None
    if not re.fullmatch(r"[0-9]{8}", picked["ord_dt"]):
        return None
    try:
        day = datetime.strptime(picked["ord_dt"], "%Y%m%d").date().isoformat()
    except ValueError:
        return None
    return (day, picked["odno"], picked["ord_gno_brno"], picked["orgn_odno"])


def evidence_pages(collection) -> list[EvidencePage]:
    """수집기 `QueryCollection`을 파서 입력으로 해동한다(raw row는 로그/저장 대상이 아니다).

    `QueryPage.rows`는 frozen mapping이라 해동하지 않으면 파서가 malformed로 읽는다.
    수집이 불완전하게 끝나도 여기서 그 사실을 지우지 않는다 — 실패한 페이지는 애초에
    수집기가 싣지 않으므로 마지막 페이지의 `tr_cont`가 F/M으로 남아 `_complete`가 거짓이 된다.
    """
    return [EvidencePage(rows=[dict(row) for row in page.rows],
                         continuation=page.response_cont,
                         next_cursor=page.next_cursor if page.next_cursor is not None else ("", ""),
                         request_cursor=tuple(page.request_cursor),
                         request_cont=page.request_cont)
            for page in collection.pages]


def parser_scope(scope, *, session: str) -> dict:
    """수집기 `QueryScope`를 파서의 query_scope 어휘로 옮긴다(daily→all, exchange_scope→exchange).

    세션은 조회 응답에 없다 — 주문의 `request_binding['session']`에서 호출자가 실어 준다.
    """
    return {"account_scope": scope.account_scope, "market": scope.market,
            "exchange": scope.exchange_scope, "start_date": scope.start_date,
            "end_date": scope.end_date, "tr_id": scope.tr_id,
            "query_kind": "all" if scope.query_kind == "daily" else scope.query_kind,
            "session": session}


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

    chain 판정은 조회한 거래소 범위에 의존한다 — KRX만 조회하면 NXT/SOR에 있는 자식행은
    보이지 않는다. 범위를 좁히는 것이 항상 fail-closed는 아니므로 호출자는 운영과 같은
    ALL 범위를 보내고(provenance는 ALL을 superset으로 허용한다), 넓혀서 보이게 된 다른
    거래소의 우리 주문 행은 수량을 싣지 않고 exchange_mismatch로 끝낸다.
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
    identities, unreadable = [], 0
    try:
        for page in pages:
            if not isinstance(page, EvidencePage) or not page.success:
                continue
            for raw in page.rows:
                identity = _identity(raw)
                if identity is None:
                    unreadable += 1
                else:
                    identities.append((identity, raw))
    except TypeError:
        return OrderEvidence(**dict(unknown, schema_valid=False, reason="malformed_row"))
    # 거래소는 매칭 키가 아니다 — 조회 범위가 ALL이면 같은 주문이 다른 거래소 라벨로 올 수 있고,
    # 그 사실은 '못 찾았다'가 아니라 아래에서 이름 붙은 exchange_mismatch로 끝나야 한다.
    wanted = (ref.order_date, ref.order_no, ref.org_no, ref.parent_order_no)
    candidates = [raw for identity, raw in identities if identity == wanted]
    # 읽히지 않은 행이 실제로 우리 주문의 자식행일 수 있다 — 자식행이 보이면 수량 해석을
    # 포기한다는 전제를 지키려면 chain 미상도 chain과 같게 처리해야 한다.
    chain = (bool(ref.parent_order_no) or bool(unreadable)
             or any(identity[3] == ref.order_no for identity, _ in identities))
    if not candidates:
        if unreadable:
            # 우리 행이 그 읽히지 않은 행일 수 있으므로 '없음'으로 단정하지 않는다.
            return OrderEvidence(**dict(unknown, schema_valid=False, reason="chain_undecidable",
                                        chain=True))
        return OrderEvidence(**dict(unknown, chain=chain))
    try:
        matches = [_parse_row(raw) for raw in candidates]
    except (ValueError, KeyError, TypeError, InvalidOperation):
        return OrderEvidence(**dict(unknown, schema_valid=False, reason="malformed_row", chain=chain))
    value = matches[0]
    if any(r != value for r in matches[1:]):
        return OrderEvidence(**dict(unknown, schema_valid=False, reason="conflicting_rows", chain=chain))
    if (value["pdno"], value["side"]) != (symbol, side):
        return OrderEvidence(**dict(unknown, schema_valid=False, reason="identity_conflict", chain=chain))
    if value["excg_id_dvsn_cd"] != ref.exchange:
        # 다른 거래소에서 체결된 주문의 수량을 이 ref로 적용하지 않는다. order_key는 사후에
        # 바꿀 수 없으므로 '행에서 읽어 채우기'가 아니라 '읽어서 대조하기'다.
        return OrderEvidence(**dict(unknown, schema_valid=False, reason="exchange_mismatch", chain=chain))
    supported = (complete and ref.market == "KR" and ref.exchange == "KRX" and not chain
                 and tr_id == "TTTC0081R" and session == "regular" and query_kind == "all"
                 and value["ord_dvsn_cd"] in ("00", "01") and value["cancel"] == "N"
                 and value["filled"] == value["qty"] and value["remaining"] == 0
                 and value["cancelled"] == 0 and value["rejected"] == 0)
    if supported:
        reason = ""
    elif not complete:
        reason = "incomplete"
    elif unreadable:
        reason = "chain_undecidable"
    else:
        reason = "unsupported_finality"
    return OrderEvidence(ref, symbol, side, value["qty"], value["filled"], value["amount"],
                         value["remaining"], value["cancelled"],
                         OrderState.FINAL_FILLED if supported else OrderState.RECONCILING,
                         complete=complete, supported_finality=supported, source_contract=source_contract,
                         reason=reason, rejected_quantity=value["rejected"], observed_at=observed_at,
                         request_started_at=request_started_at, query_scope=query_scope, chain=chain)
