#!/usr/bin/env python3
"""H3 자료 수집 — OpenDART 사업보고서 주요계정(자본총계·당기순이익) FY2014~FY2025 (사전 등록 §3-3).

다중회사 API(fnlttMultiAcnt, 회사 100개/호출)로 호출 수를 줄인다. 키는 환경변수 DART_API_KEY 로만 읽고 어디에도 쓰지 않는다.
실행(coordinator 전용, 키 필요): DART_API_KEY 가 있는 환경에서
  venv/bin/python scripts/research/dart_fundamentals.py --out results/index_plus_h3
출력: dart_fundamentals.csv (fy, corp_code, stock_code, fs_div, rcept_no, rcept_date, equity, net_income), dart_meta.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

URL = "https://opendart.fss.or.kr/api/fnlttMultiAcnt.json"
CORPCODE_XML = Path.home() / ".cache" / "ai_trader" / "dart_corp_code.xml"
YEARS = range(2014, 2026)
BATCH = 100
CALL_BUDGET = 8000
ACCOUNTS = {"자본총계": "equity", "당기순이익": "net_income", "당기순이익(손실)": "net_income"}


def candidates() -> list:
    """H2 와 같은 제외 규칙의 KOSPI 상장 종목 (현재 목록 — 생존 편향)."""
    import FinanceDataReader as fdr
    from index_plus_h2 import exclusion_reason
    lst = fdr.StockListing("KOSPI")
    out = []
    for _, r in lst.iterrows():
        code, name = str(r["Code"]).zfill(6), str(r["Name"])
        if exclusion_reason(code, name) is None:
            out.append({"code": code, "name": name, "marcap": r.get("Marcap"), "stocks": r.get("Stocks")})
    return out


def corp_map() -> dict:
    root = ET.fromstring(CORPCODE_XML.read_bytes())
    m = {}
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if len(sc) == 6:
            m[sc] = el.findtext("corp_code").strip()
    return m


def fetch(key: str, codes: list, fy: int, calls: list) -> list:
    if len(calls) >= CALL_BUDGET:
        raise SystemExit("호출 상한 도달")
    for attempt in range(3):
        r = requests.get(URL, params={"crtfc_key": key, "corp_code": ",".join(codes), "bsns_year": str(fy),
                                      "reprt_code": "11011"}, timeout=30)
        calls.append(r.status_code)
        if r.status_code == 200:
            d = r.json()
            if d.get("status") == "000":
                return d.get("list", [])
            if d.get("status") == "013":          # 조회 자료 없음
                return []
            raise RuntimeError(f"DART status {d.get('status')}: {d.get('message')}")
        time.sleep(2)
    raise RuntimeError(f"HTTP {r.status_code}")


def to_num(s):
    try:
        return int(str(s).replace(",", "").strip())
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/index_plus_h3")
    args = ap.parse_args()
    key = os.environ.get("DART_API_KEY", "")
    if not key:
        raise SystemExit("DART_API_KEY 없음")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cands = candidates()
    cmap = corp_map()
    mapped = [c for c in cands if c["code"] in cmap]
    corps = sorted({cmap[c["code"]] for c in mapped})
    calls, rows, errors = [], {}, []
    t0 = time.time()
    for fy in YEARS:
        for i in range(0, len(corps), BATCH):
            batch = corps[i:i + BATCH]
            try:
                items = fetch(key, batch, fy, calls)
            except Exception as e:                      # noqa: BLE001 — 배치 단위로 기록하고 계속
                errors.append({"fy": fy, "batch": i // BATCH, "error": str(e)[:200]})
                continue
            for it in items:
                acct = ACCOUNTS.get(str(it.get("account_nm", "")).strip())
                if acct is None:
                    continue
                k = (fy, it["corp_code"], it.get("fs_div"))
                row = rows.setdefault(k, {"fy": fy, "corp_code": it["corp_code"], "stock_code": it.get("stock_code"),
                                          "fs_div": it.get("fs_div"), "rcept_no": it.get("rcept_no"),
                                          "rcept_date": str(it.get("rcept_no", ""))[:8],
                                          "equity": None, "net_income": None})
                if row[acct] is None:
                    row[acct] = to_num(it.get("thstrm_amount"))
            time.sleep(0.3)
    with (out / "dart_fundamentals.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["fy", "corp_code", "stock_code", "fs_div", "rcept_no", "rcept_date",
                                          "equity", "net_income"])
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])
    with (out / "universe_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["code", "name", "marcap", "stocks", "corp_code"])
        w.writeheader()
        for c in cands:
            w.writerow({**c, "corp_code": cmap.get(c["code"])})
    meta = {"candidates": len(cands), "mapped": len(mapped), "corps": len(corps), "years": [YEARS[0], YEARS[-1]],
            "calls": len(calls), "http_codes": {str(c): calls.count(c) for c in set(calls)}, "rows": len(rows),
            "rows_with_both": sum(1 for r in rows.values() if r["equity"] is not None and r["net_income"] is not None),
            "errors": errors, "elapsed_s": round(time.time() - t0, 1), "api": "fnlttMultiAcnt 11011"}
    (out / "dart_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in meta.items() if k != "errors"}, ensure_ascii=False), "errors", len(errors))


if __name__ == "__main__":
    main()
