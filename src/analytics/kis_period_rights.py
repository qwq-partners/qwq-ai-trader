"""KIS 기간별계좌권리현황(CTRGA011R) 읽기 전용 수집 — 월 단위·전체 페이지·완료 판정 (66차).

조회 성공은 회계상 소득 확정이 아니다. `last_alct_amt` 의 세전/세후·실제 지급·보존기간·예수금/CMA 이자 포함
여부는 지급 증빙과 대사하기 전 확정하지 않는다. 권리 이벤트의 현금은 일반 입출금·계좌간 이체로 분류하지 않는다.
공식 샘플의 재귀 상한 도달은 완료가 아니라 `partial_max_pages` 다.
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

SCHEMA_VERSION = "kis-period-rights-collection-v1"
TR_ID = "CTRGA011R"
NEXT_PAGE = ("F", "M")
LAST_PAGE = ("D", "E", "")   # 공식 저장소: D/E 마지막. 헤더 부재("")는 마지막으로 보되 상태에 남긴다

Fetch = Callable[..., Awaitable[Dict[str, Any]]]


def month_windows(start: date, end: date) -> List[Tuple[str, str]]:
    """[start, end] 을 월 단위 (YYYYMMDD, YYYYMMDD) 창으로 나눈다. 끝은 end 로 자른다."""
    if start > end:
        raise ValueError("start 가 end 보다 뒤다")
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        first = date(y, m, 1) if (y, m) != (start.year, start.month) else start
        last_day = date(y, m, calendar.monthrange(y, m)[1])
        last = min(last_day, end)
        out.append((first.strftime("%Y%m%d"), last.strftime("%Y%m%d")))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


VALID_TR_CONT = {"F", "M", "D", "E", ""}


def _output(data: Dict[str, Any]) -> Any:
    output = data.get("output")
    return output if output is not None else data.get("output1")


def classify_page(data: Any) -> str:
    """한 페이지 응답 분류: ok / empty / error / malformed.

    행이 전부 dict 가 아니거나, `_tr_cont` 가 F/M/D/E/부재 밖이거나, 연속조회 키가 문자열이 아니면 malformed —
    비정상 응답을 '완전 조회' 로 판정하지 않는다(리뷰 P2).
    """
    if not isinstance(data, dict):
        return "malformed"
    rt_cd = data.get("rt_cd")
    if not isinstance(rt_cd, str):
        return "malformed"
    if rt_cd != "0":
        return "error"
    output = _output(data)
    if not isinstance(output, list) or any(not isinstance(r, dict) for r in output):
        return "malformed"
    cont = data.get("_tr_cont")
    if cont is not None and (not isinstance(cont, str) or cont.upper() not in VALID_TR_CONT):
        return "malformed"
    for key in ("ctx_area_fk100", "ctx_area_nk100"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            return "malformed"
    return "ok" if output else "empty"


def _rows(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    output = _output(data)
    return list(output) if isinstance(output, list) else []


async def collect_window(fetch: Fetch, start_yyyymmdd: str, end_yyyymmdd: str, *, max_pages: int = 50,
                         pause: Optional[Callable[[], Awaitable[None]]] = None) -> Dict[str, Any]:
    """한 조회 창의 모든 페이지를 회수한다. fetch(start, end, fk100=, nk100=, tr_cont=) -> 응답 dict.

    status: complete / empty / partial_max_pages / partial_page_repeat / error / malformed
    """
    pages: List[Dict[str, Any]] = []
    raw_pages: List[Any] = []            # 비공개 파일에 원본 응답 전체를 보존한다 — 재검증용(리뷰 P2)
    rows: List[Dict[str, Any]] = []
    seen_keys = set()
    fk, nk, tr_cont = "", "", ""
    status = "complete"
    header_missing = False
    for page_no in range(1, max_pages + 1):
        if pause is not None and page_no > 1:
            await pause()
        data = await fetch(start_yyyymmdd, end_yyyymmdd, fk100=fk, nk100=nk, tr_cont=tr_cont)
        raw_pages.append(data)
        kind = classify_page(data)
        pages.append({"page": page_no, "kind": kind,
                      "rt_cd": data.get("rt_cd") if isinstance(data, dict) else None,
                      "msg_cd": data.get("msg_cd") if isinstance(data, dict) else None,
                      "msg1": (str(data.get("msg1"))[:120] if isinstance(data, dict) and data.get("msg1") is not None else None),
                      "tr_cont": data.get("_tr_cont") if isinstance(data, dict) else None,
                      "rows": len(_rows(data)) if isinstance(data, dict) else 0})
        if kind in ("error", "malformed"):
            status = kind
            break
        rows.extend(_rows(data))
        cont_raw = data.get("_tr_cont")
        cont = cont_raw.upper() if isinstance(cont_raw, str) else ""
        if cont in NEXT_PAGE:
            next_fk = data.get("ctx_area_fk100") if isinstance(data.get("ctx_area_fk100"), str) else ""
            next_nk = data.get("ctx_area_nk100") if isinstance(data.get("ctx_area_nk100"), str) else ""
            key = (next_fk, next_nk)
            if key in seen_keys or key == (fk, nk):
                status = "partial_page_repeat"          # 같은 연속조회 키가 반복 — 무한 순회 방지
                break
            seen_keys.add(key)
            fk, nk, tr_cont = next_fk, next_nk, "N"
            if page_no == max_pages:
                status = "partial_max_pages"            # 공식 샘플의 재귀 상한 도달 ≠ 완료
            continue
        if cont == "":
            header_missing = True
        break
    else:
        status = "partial_max_pages"
    if status == "complete" and not rows:
        status = "empty"
    return {"start": start_yyyymmdd, "end": end_yyyymmdd, "status": status, "pages": pages,
            "api_calls": len(pages), "rows": rows, "tr_cont_header_missing": header_missing,
            "raw_pages": raw_pages}


def _decimal(value: Any) -> Optional[Decimal]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        d = Decimal(value.strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def summarize_rights(windows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """집계만 한다(해석 없음). 금액 합은 Decimal 문자열, 파싱 불가 금액은 unparsed 로 센다."""
    by_type: Dict[str, Dict[str, Any]] = {}
    statuses: Dict[str, int] = {}
    total_rows = 0
    unparsed = 0
    for w in windows:
        statuses[w["status"]] = statuses.get(w["status"], 0) + 1
        for r in w.get("rows", []):
            total_rows += 1
            t = str(r.get("rght_type_cd") if r.get("rght_type_cd") is not None else "<none>")
            bucket = by_type.setdefault(t, {"rows": 0, "last_alct_amt_sum": Decimal("0"), "unparsed_amount_rows": 0,
                                             "symbols": set()})
            bucket["rows"] += 1
            amt = _decimal(r.get("last_alct_amt"))
            if amt is None:
                bucket["unparsed_amount_rows"] += 1
                unparsed += 1
            else:
                bucket["last_alct_amt_sum"] += amt
            if isinstance(r.get("pdno"), str):
                bucket["symbols"].add(r["pdno"])
    return {
        "schema_version": SCHEMA_VERSION, "tr_id": TR_ID,
        "windows": len(windows), "window_statuses": statuses,
        "complete_windows": statuses.get("complete", 0) + statuses.get("empty", 0),
        "rows": total_rows, "unparsed_amount_rows": unparsed,
        "by_right_type": {t: {"rows": b["rows"], "last_alct_amt_sum": str(b["last_alct_amt_sum"]),
                              "unparsed_amount_rows": b["unparsed_amount_rows"], "symbol_count": len(b["symbols"])}
                          for t, b in sorted(by_type.items())},
        # 해석 금지 사항을 요약에도 남긴다
        "caveats": ["조회 성공은 소득 확정이 아니다", "last_alct_amt 의 세전/세후·실제 지급·보존기간은 미확인",
                    "예수금/CMA 이자 포함 여부 미확인", "권리 현금은 외부 입출금이 아니다",
                    "partial_* 창은 완전 조회가 아니다"],
    }
