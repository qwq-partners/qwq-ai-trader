"""실거래 KODEX200 초과수익 원장 (설계 A, 계획 1단계 T1~T3).

합성 포지션·합성 DB 행·가짜 브로커만 쓴다. 경로는 전부 tmp_path — 운영 캐시·네트워크 무접촉.
async 는 asyncio.run() 으로 직접 감싼다(저장소 관례).

실행: venv/bin/python -m pytest tests/test_excess_return_ledger.py -q -p no:cacheprovider
"""

import asyncio
import importlib.util
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analytics import excess_return as er  # noqa: E402


def _load_script(name):
    spec = importlib.util.spec_from_file_location(f"_{name}_for_excess_test", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


exporter = _load_script("export_risk_ledger")
canary = _load_script("review_risk_canary")

D = Decimal
COMPUTED_AT = "2026-09-10T20:31:00"
# 2026-09-01(화) ~ 09-10(목) 거래일 종가 — 100 부터 하루 +1 이 아니라 손 계산이 쉬운 값
BENCH = {
    "2026-09-01": D("100"), "2026-09-02": D("101"), "2026-09-03": D("102"),
    "2026-09-04": D("103"), "2026-09-07": D("104"), "2026-09-08": D("105"),
    "2026-09-09": D("106"), "2026-09-10": D("107"),
}


# ── 포지션 dict 빌더 (exporter build_ledger 출력과 같은 모양) ─────────────────────

def _pos(pid="P1", *, entry="2026-09-01", buy_qty=100, buy_price="10000", buy_fee="140",
         sells=(("2026-09-03", 100, "10500", "2236", "trailing"),), net_pnl="41000",
         status="closed", strategy="sepa_trend", stop="5.0", legacy=False,
         lots_ambiguous=False, exits_aggregated=False, entry_reason="", exit_type=None,
         pnl_missing=False):
    fills = [{"ts": f"{entry}T09:05:00", "side": "buy", "price": buy_price,
              "quantity": buy_qty, "fee": buy_fee}]
    exits = []
    for d, q, price, fee, reason in sells:
        row = {"ts": f"{d}T14:30:00", "price": price, "quantity": q, "fee": fee, "reason": reason}
        exits.append(row)
        fills.append({k: row[k] for k in ("ts", "price", "quantity", "fee")} | {"side": "sell"})
    entry_risk = None if legacy else {"sizing_mode": "risk", "stop_pct": stop, "cohort_id": "risk-sepa_trend-v1"}
    return {
        "position_id": pid, "symbol": "005930", "strategy": strategy,
        "cohort_id": "legacy-unmeasured" if legacy else "risk-sepa_trend-v1",
        "applied_sha": "abc", "status": status, "entry_risk": entry_risk,
        "initial_risk_amount": None, "planned_vs_filled_risk_delta": None,
        "fills": fills, "exits": exits, "net_pnl": net_pnl,
        "actual_stop_pct": None if legacy else stop,
        "lots_ambiguous": lots_ambiguous or exits_aggregated, "exits_aggregated": exits_aggregated,
        "entry_reason": entry_reason,
        "exit_type": exit_type if exit_type is not None else (sells[-1][4] if sells else ""),
        "pnl_missing": pnl_missing,
    }


def _row(pos, bench=BENCH, day_status=None):
    return er.position_row(pos, bench, day_status or {}, computed_at=COMPUTED_AT, code_sha="sha1")


# ── 1. 손 계산 일치 ───────────────────────────────────────────────────────────

def test_single_exit_hand_calculation():
    row = _row(_pos())
    assert row["schema"] == 1 and row["exclusion"] is None
    assert row["entry_cost"] == "1000000" and row["net_pnl"] == "41000"
    assert D(row["net_return"]) == D("0.041")
    assert D(row["bench_return"]) == D("0.02")          # 102/100 - 1
    assert D(row["excess_return"]) == D("0.021")
    assert D(row["excess_krw"]) == D("21000")           # 41000 - 1,000,000 × 0.02
    assert row["clip_basis"] == "entry_stop" and D(row["clip_pct"]) == D("5")
    assert D(row["clipped_return"]) == D("0.041") and D(row["stop_overshoot"]) == 0
    assert row["entry_date"] == "2026-09-01" and row["last_exit_date"] == "2026-09-03"
    assert row["holding_days"] == 2
    assert row["entry_quality"] == "fill" and row["exit_quality"] == "fill"
    assert row["bench_missing_reason"] is None
    assert D(row["fees_total_est"]) == D("2376")
    assert row["computed_at"] == COMPUTED_AT and row["code_sha"] == "sha1"


def test_split_exit_weighted_by_quantity():
    pos = _pos(sells=(("2026-09-03", 40, "10500", "895", "take_profit"),
                      ("2026-09-07", 60, "11000", "1406", "trailing")), net_pnl="77559")
    row = _row(pos)
    # 0.4×(102/100−1) + 0.6×(104/100−1) = 0.008 + 0.024
    assert D(row["bench_return"]) == D("0.032")
    assert row["exit_types"] == ["take_profit", "trailing"]
    assert row["last_exit_date"] == "2026-09-07"


def test_same_day_round_trip_bench_is_zero():
    row = _row(_pos(sells=(("2026-09-01", 100, "10100", "2151", "stop_loss"),), net_pnl="7709"))
    assert D(row["bench_return"]) == 0
    assert D(row["excess_return"]) == D(row["net_return"])


def test_benchmark_date_missing_is_null_not_zero():
    row = _row(_pos(sells=(("2026-09-05", 100, "10500", "2236", "trailing"),)))  # 09-05 토요일, 캐시 없음
    assert row["exclusion"] is None
    assert row["bench_return"] is None and row["excess_return"] is None
    assert row["excess_krw"] is None and row["clipped_excess_return"] is None
    assert row["bench_missing_reason"].startswith("benchmark_date_missing")
    assert D(row["net_return"]) == D("0.041")           # 자기 수익률은 그대로


def test_clip_entry_stop_reports_overshoot():
    row = _row(_pos(net_pnl="-80000"))
    assert D(row["net_return"]) == D("-0.08")
    assert D(row["clipped_return"]) == D("-0.05")
    assert D(row["stop_overshoot"]) == D("0.03")
    assert D(row["clipped_excess_return"]) == D("-0.07")   # -0.05 - 0.02


def test_clip_common5_does_not_report_overshoot():
    row = _row(_pos(net_pnl="-80000", legacy=True))
    assert row["clip_basis"] == "common_5" and D(row["clip_pct"]) == D("5")
    assert D(row["clipped_return"]) == D("-0.05")
    assert row["stop_overshoot"] is None


def test_clip_uses_entry_risk_stop_when_actual_missing():
    pos = _pos(net_pnl="-80000", stop="4.0")
    pos["actual_stop_pct"] = None
    row = _row(pos)
    assert row["clip_basis"] == "entry_stop" and D(row["clip_pct"]) == D("4")
    assert D(row["clipped_return"]) == D("-0.04")


# ── 동기화 판정 ────────────────────────────────────────────────────────────────

def test_is_sync_rules():
    assert er.is_sync_entry("KIS_SYNC_005930_20260901", "KIS 동기화 복구")
    assert er.is_sync_entry("SYNC_AAPL_1", "")
    assert er.is_sync_entry("KR-1", "sync_detected")
    assert not er.is_sync_entry("KR-1", "")
    assert not er.is_sync_entry(None, None)
    for t in ("kis_sync", "sync_reconcile", "sync_closed", "sync_partial", "sync_detected"):
        assert er.is_sync_exit(t)
    assert not er.is_sync_exit("trailing") and not er.is_sync_exit(None)


def test_sync_entry_excluded_and_sync_exit_included_as_estimated():
    row = _row(_pos("KIS_SYNC_005930_20260901", entry_reason="KIS 동기화 복구"))
    assert row["exclusion"] == "sync_entry" and row["entry_quality"] == "sync_estimated"

    row = _row(_pos(sells=(("2026-09-03", 100, "10500", "2236", "sync_detected"),)))
    assert row["exclusion"] is None and row["exit_quality"] == "sync_estimated"
    row = _row(_pos(sells=(("2026-09-02", 50, "10500", "0", "trailing"),
                           ("2026-09-03", 50, "10500", "0", "kis_sync"))))
    assert row["exit_quality"] == "sync_estimated"


# ── 2. 종결 판정 ───────────────────────────────────────────────────────────────

def test_awaiting_close_then_closed_next_run():
    pos = _pos(buy_qty=150)                                  # 첫 체결 100 만 팔림
    assert er.classify(pos, BENCH, {}) == "awaiting_close"
    assert _row(pos) is None
    pos2 = _pos(buy_qty=150, sells=(("2026-09-03", 100, "10500", "0", "trailing"),
                                    ("2026-09-04", 50, "10600", "0", "trailing")))
    assert er.classify(pos2, BENCH, {}) is None
    assert _row(pos2)["exclusion"] is None


def test_open_position_has_no_row():
    assert er.classify(_pos(status="open"), BENCH, {}) == "open"
    assert _row(_pos(status="open")) is None


def test_quantity_mismatch():
    pos = _pos(sells=(("2026-09-03", 60, "10500", "0", "trailing"), ("2026-09-04", 60, "10500", "0", "trailing")))
    assert er.classify(pos, BENCH, {}) == "quantity_mismatch"


def test_exits_aggregated_checked_before_lots_ambiguous():
    assert er.classify(_pos(exits_aggregated=True), BENCH, {}) == "exits_aggregated"
    assert er.classify(_pos(lots_ambiguous=True), BENCH, {}) == "lots_ambiguous"


def test_exits_missing_checked_first():
    assert er.classify(_pos(sells=()), BENCH, {}) == "exits_missing"
    assert er.classify(_pos(net_pnl=None), BENCH, {}) == "exits_missing"
    assert er.classify(_pos(pnl_missing=True), BENCH, {}) == "exits_missing"
    row = _row(_pos(pnl_missing=True, net_pnl="0"))
    assert row["exclusion"] == "exits_missing" and row["net_pnl"] is None   # NULL 을 0원으로 쓰지 않는다


def test_record_incomplete_and_bench_out_of_range():
    assert er.classify(_pos(), BENCH, {"2026-09-02": "incomplete"}) == "record_incomplete"
    assert er.classify(_pos(), BENCH, {date(2026, 9, 2): "incomplete"}) == "record_incomplete"
    assert er.classify(_pos(), BENCH, {"2026-09-04": "incomplete"}) is None   # 보유 구간 밖
    assert er.classify(_pos(), BENCH, {"2026-09-02": "complete"}) is None
    assert er.classify(_pos(entry="2026-08-28"), BENCH, {}) == "bench_out_of_range"


def test_day_status_gap_counts_only_missing_days():
    full = {d: "complete" for d in BENCH}
    assert er.has_day_status_gap(_pos(), BENCH, full) is False
    partial = dict(full)
    del partial["2026-09-02"]
    assert er.has_day_status_gap(_pos(), BENCH, partial) is True
    assert er.has_day_status_gap(_pos(), BENCH, {}) is True


# ── 합성 DB 행 → fetch_trade_records → build_ledger (exporter 경로) ───────────────

def _trow(tid, symbol, entry, qty=100, price=10000, *, strategy="sepa_trend", entry_reason="",
          exit_time=None, exit_price=None, exit_qty=0, exit_type="", pnl=None, ctx=None):
    return {
        "id": tid, "symbol": symbol, "name": "", "entry_time": datetime.fromisoformat(entry),
        "entry_price": price, "entry_quantity": qty, "entry_reason": entry_reason,
        "entry_strategy": strategy, "entry_signal_score": 0, "market_context": json.dumps(ctx or {}),
        "exit_time": datetime.fromisoformat(exit_time) if exit_time else None,
        "exit_price": exit_price, "exit_quantity": exit_qty, "exit_reason": "", "exit_type": exit_type,
        "pnl": pnl, "pnl_pct": 0, "holding_minutes": 0,
    }


def _leg(tid, ts, qty, price, exit_type="trailing"):
    return {"trade_id": tid, "event_time": datetime.fromisoformat(ts), "price": price,
            "quantity": qty, "exit_type": exit_type, "exit_reason": ""}


def _fake_fetch(trade_rows, leg_rows, calls=None):
    async def fetch(sql, *args):
        if calls is not None:
            calls.append(sql)
        if "FROM trades" in sql:
            return list(trade_rows)
        if "FROM trade_events" in sql:
            return [lg for lg in leg_rows if lg["trade_id"] in args[0]]
        raise AssertionError(f"예상 밖 SQL: {sql}")
    return fetch


def _db_fixture():
    """제외 사유 9종 + 대기 1 + 포함 2 — 종목을 달리해 lots_ambiguous 는 의도한 쌍만 걸리게 한다."""
    T, L = [], []

    def closed(tid, sym, entry, exit_ts, pnl, **kw):
        legs = kw.pop("legs", [(exit_ts, 100, 10500)])
        T.append(_trow(tid, sym, entry, exit_time=exit_ts, exit_price=10500,
                       exit_qty=sum(q for _, q, _ in legs), exit_type="trailing", pnl=pnl, **kw))
        for ts, q, p in legs:
            L.append(_leg(tid, ts, q, p))

    closed("OK1", "A00001", "2026-09-01T09:05:00", "2026-09-03T14:00:00", 47000)
    closed("OK0", "A00002", "2026-09-02T09:05:00", "2026-09-04T14:00:00", 0)           # 실제 0원
    closed("NULLPNL", "A00003", "2026-09-02T09:05:00", "2026-09-04T14:00:00", None)    # 원천 NULL
    closed("QTY", "A00004", "2026-09-01T09:05:00", "2026-09-03T14:00:00", 1000,
           legs=[("2026-09-02T14:00:00", 60, 10500), ("2026-09-03T14:00:00", 60, 10500)])
    # 분할 매도 leg 없음 + 단일 재구성과 누적 pnl 불일치 → exits_aggregated (lots_ambiguous 도 켜짐)
    T.append(_trow("AGG", "A00005", "2026-09-01T09:05:00", exit_time="2026-09-03T14:00:00",
                   exit_price=10500, exit_qty=100, exit_type="trailing", pnl=30000))
    # 같은 종목 보유 구간 겹침 → 둘 다 lots_ambiguous
    closed("AMB1", "A00006", "2026-09-01T09:05:00", "2026-09-04T14:00:00", 2000)
    closed("AMB2", "A00006", "2026-09-02T09:05:00", "2026-09-03T14:00:00", 3000)
    closed("MAN", "A00007", "2026-09-01T09:05:00", "2026-09-03T14:00:00", 4000, strategy="manual")
    closed("KIS_SYNC_A00008_20260901", "A00008", "2026-09-01T09:05:00", "2026-09-03T14:00:00", 5000,
           entry_reason="KIS 동기화 복구")
    closed("REC", "A00009", "2026-09-03T14:00:00", "2026-09-03T14:00:00", 6000, entry_reason="recovered_at_exit")
    closed("OOR", "A00010", "2026-08-27T09:05:00", "2026-09-03T14:00:00", 7000)
    closed("INC", "A00011", "2026-09-07T09:05:00", "2026-09-09T14:00:00", 8000)
    # 다중 체결 진입(스냅샷 누적 150) 중 100 만 팔림 → awaiting_close
    closed("WAIT", "A00012", "2026-09-01T09:05:00", "2026-09-03T14:00:00", 9000,
           ctx={"entry_risk": {"filled_quantity": 150, "entry_cost": "1500000"}})
    # 보유 중
    T.append(_trow("OPEN", "A00013", "2026-09-08T09:05:00"))
    return T, L


def _ledger_from_db(T, L):
    trades = asyncio.run(exporter.fetch_trade_records(_fake_fetch(T, L), 730))
    return trades, exporter.build_ledger(trades, {})["positions"]


def test_exclusion_reasons_via_exporter_and_excluded_sums():
    T, L = _db_fixture()
    _, positions = _ledger_from_db(T, L)
    day_status = {"2026-09-08": "incomplete"}
    got = {p["position_id"]: er.classify(p, BENCH, day_status) for p in positions}
    assert got == {
        "OK1": None, "OK0": None, "NULLPNL": "exits_missing", "QTY": "quantity_mismatch",
        "AGG": "exits_aggregated", "AMB1": "lots_ambiguous", "AMB2": "lots_ambiguous",
        "MAN": "manual_entry", "KIS_SYNC_A00008_20260901": "sync_entry", "REC": "recovered_at_exit",
        "OOR": "bench_out_of_range", "INC": "record_incomplete", "WAIT": "awaiting_close", "OPEN": "open",
    }
    rows = [r for r in (er.position_row(p, BENCH, day_status, computed_at=COMPUTED_AT, code_sha="s")
                        for p in positions) if r is not None]
    summary = er.summarize(rows, [], today=date(2026, 9, 10), awaiting_close=1, day_status_missing=0)
    excluded = summary["windows"]["all"]["all"]["excluded"]
    assert set(excluded) == {"exits_missing", "quantity_mismatch", "exits_aggregated", "lots_ambiguous",
                             "manual_entry", "sync_entry", "recovered_at_exit", "bench_out_of_range",
                             "record_incomplete"}
    for reason, agg in excluded.items():
        mine = [r for r in rows if r["exclusion"] == reason]
        assert agg["n"] == len(mine)
        known = [D(r["net_pnl"]) for r in mine if r["net_pnl"] is not None]
        assert agg["net_pnl_missing"] == len(mine) - len(known)
        assert (agg["net_pnl_sum"] is None) if not known else (D(agg["net_pnl_sum"]) == sum(known, D(0)))
    assert summary["windows"]["all"]["all"]["n"] == 2
    assert summary["awaiting_close"] == 1


def test_null_pnl_is_not_real_zero():
    T, L = _db_fixture()
    trades, positions = _ledger_from_db(T, L)
    by_id = {t.id: t for t in trades}
    assert by_id["NULLPNL"].pnl_missing is True and by_id["OK0"].pnl_missing is False
    rows = {p["position_id"]: _row(p) for p in positions}
    assert rows["NULLPNL"]["exclusion"] == "exits_missing" and rows["NULLPNL"]["net_pnl"] is None
    assert rows["OK0"]["exclusion"] is None and D(rows["OK0"]["net_pnl"]) == 0


def test_accounting_rows_match_db_pnl():
    T, L = _db_fixture()
    _, positions = _ledger_from_db(T, L)
    rows = [r for r in (_row(p) for p in positions) if r is not None]
    ids = {r["position_id"] for r in rows}
    db_sum = sum((D(str(t["pnl"])) for t in T if t["id"] in ids and t["pnl"] is not None), D(0))
    row_sum = sum((D(r["net_pnl"]) for r in rows if r["net_pnl"] is not None), D(0))
    assert abs(db_sum - row_sum) <= 1
    assert "WAIT" not in ids and "OPEN" not in ids


def test_exporter_exit_price_zero_uses_sell_legs():
    """DB 직접 부분매도 경로 — trades.exit_price NULL/0 이어도 SELL leg 가 있으면 leg 로 exits 를 만든다."""
    T = [_trow("Z1", "B00001", "2026-09-01T09:05:00", exit_time=None, exit_price=None,
               exit_qty=100, exit_type="", pnl=40000)]
    L = [_leg("Z1", "2026-09-02T10:00:00", 40, 10400), _leg("Z1", "2026-09-03T10:00:00", 60, 10500)]
    _, positions = _ledger_from_db(T, L)
    pos = positions[0]
    assert [e["quantity"] for e in pos["exits"]] == [40, 60]
    assert pos["status"] == "closed" and pos["exits_aggregated"] is False
    # leg 도 없고 exit_price 도 없으면 종전처럼 exits 없음
    T2 = [_trow("Z2", "B00002", "2026-09-01T09:05:00", exit_price=None, exit_qty=100, pnl=1)]
    _, positions2 = _ledger_from_db(T2, [])
    assert positions2[0]["exits"] == []


def test_fetch_trade_records_does_not_touch_pool_lifecycle():
    calls = []
    T, L = _db_fixture()
    asyncio.run(exporter.fetch_trade_records(_fake_fetch(T, L, calls), 30))
    assert len(calls) == 2   # SELECT 두 개뿐 — connect/disconnect 없음(fetch 만 받는다)


def test_load_from_db_still_connects_and_disconnects():
    T, L = _db_fixture()
    events = []

    class _Storage:
        pool = None

        async def connect(self):
            events.append("connect")
            self.pool = SimpleNamespace(fetch=_fake_fetch(T, L))

        async def disconnect(self):
            events.append("disconnect")

    trades = asyncio.run(exporter._load_from_db(_Storage(), 30))
    assert events == ["connect", "disconnect"] and len(trades) == len(T)


# ── 요약·한 줄 ────────────────────────────────────────────────────────────────

def _included_rows(n, excess_pnl="41000", month_day="2026-09-03"):
    return [_row(_pos(f"P{i}", net_pnl=excess_pnl, sells=((month_day, 100, "10500", "0", "trailing"),)))
            for i in range(n)]


def test_summarize_status_windows_and_groups():
    rows = _included_rows(29)
    s = er.summarize(rows, [], today=date(2026, 9, 10), awaiting_close=0, day_status_missing=0)
    m = s["windows"]["all"]["all"]
    assert m["status"] == "insufficient_sample" and m["n"] == 29
    assert D(m["mean_excess"]) == D("0.021") and D(m["median_excess"]) == D("0.021")
    assert D(m["excess_krw_sum"]) == D("21000") * 29
    assert D(m["excess_krw_excl_top3"]) == D("21000") * 26
    assert D(m["beat_rate"]) == 1
    assert m["t_excess"] is None                     # 분산 0 → 정의 안 됨
    for key in ("strategy:sepa_trend", "month:2026-09", "exit_quality:fill", "cohort:risk-sepa_trend-v1"):
        assert s["windows"]["all"][key]["n"] == 29
    s30 = er.summarize(_included_rows(30), [], today=date(2026, 9, 10), awaiting_close=0, day_status_missing=0)
    assert s30["windows"]["all"]["all"]["status"] == "measured"
    # 최근 90일 창 — 마지막 청산일 기준
    s_late = er.summarize(rows, [], today=date(2026, 12, 31), awaiting_close=0, day_status_missing=0)
    assert s_late["windows"]["recent_90d"]["all"]["n"] == 0
    assert s_late["windows"]["all"]["all"]["n"] == 29


def test_summarize_t_and_overshoot():
    rows = [_row(_pos("A", net_pnl="41000")), _row(_pos("B", net_pnl="-80000"))]
    m = er.summarize(rows, [], today=date(2026, 9, 10), awaiting_close=0, day_status_missing=0)["windows"]["all"]["all"]
    assert D(m["mean_excess"]) == D("-0.0395")       # (0.021 + -0.1)/2
    assert m["t_excess"] is not None
    assert m["overshoot_n"] == 1 and D(m["overshoot_krw_sum"]) == D("30000")
    assert D(m["mean_clipped_excess"]) == D("-0.0245")   # (0.021 + -0.07)/2
    assert D(m["mean_net_return"]) == D("-0.0195") and D(m["mean_bench_return"]) == D("0.02")


def test_summarize_drift_against_previous_rows():
    rows = [_row(_pos("A")), _row(_pos("B"))]
    prev = [dict(rows[0]), dict(rows[1], net_pnl="1")]
    s = er.summarize(rows, prev, today=date(2026, 9, 10), awaiting_close=0, day_status_missing=0)
    assert s["drift"] == 1


def test_format_weekly_line():
    s = er.summarize(_included_rows(3), [], today=date(2026, 9, 10), awaiting_close=0, day_status_missing=0)
    line = er.format_weekly_line(s)
    assert "09-10 계산" in line and "n=3" in line and "표본 판정 보류" in line
    assert "+2.10%" in line and "+63,000원" in line


# ── 벤치마크 변환·로드 ─────────────────────────────────────────────────────────

def test_kis_rows_to_bench_validates():
    rows = [{"date": "20260901", "close": 100.0}, {"date": "2026091", "close": 1.0},
            {"date": "20260902", "close": 0}, {"date": "20260903", "close": "x"},
            {"date": "20260904", "close": 102.5}]
    assert er.kis_rows_to_bench(rows) == [("2026-09-01", D("100.0")), ("2026-09-04", D("102.5"))]


def test_canary_uses_shared_benchmark_functions():
    assert canary.position_benchmark is er.position_benchmark
    assert canary.load_benchmark is er.load_benchmark
    assert canary._dec is er.to_decimal and canary._date is er.parse_date and canary._buys is er.buy_fills


def test_module_has_no_cache_path_constant():
    src = (ROOT / "src" / "analytics" / "excess_return.py").read_text(encoding="utf-8")
    assert ".cache" not in src and "Path.home" not in src


# ── run_daily_update: 캐시 교체·멱등·drift ────────────────────────────────────

def _kis_rows(start="2026-09-01"):
    return [{"date": d.replace("-", ""), "close": float(c)} for d, c in BENCH.items() if d >= start]


def _update(tmp_path, T, L, rows, now, broker=None, day_status=None):
    broker = broker or SimpleNamespace(get_daily_prices=AsyncMock(return_value=rows))
    summary = asyncio.run(er.run_daily_update(
        broker=broker, fetch=_fake_fetch(T, L), out_dir=tmp_path, root=ROOT, now=now,
        code_sha="sha1", day_status=day_status or {}))
    return summary, broker


def _simple_db(pnl=47000):
    T = [_trow("OK1", "A00001", "2026-09-01T09:05:00", exit_time="2026-09-03T14:00:00",
               exit_price=10500, exit_qty=100, exit_type="trailing", pnl=pnl),
         _trow("OK2", "A00002", "2026-09-02T09:05:00", exit_time="2026-09-04T14:00:00",
               exit_price=10500, exit_qty=100, exit_type="trailing", pnl=2000)]
    L = [_leg("OK1", "2026-09-03T14:00:00", 100, 10500), _leg("OK2", "2026-09-04T14:00:00", 100, 10500)]
    return T, L


def test_run_daily_update_writes_files_and_is_idempotent(tmp_path):
    T, L = _simple_db()
    now = datetime(2026, 9, 10, 20, 31)
    s1, broker = _update(tmp_path, T, L, _kis_rows(), now)
    broker.get_daily_prices.assert_awaited_once_with("069500", days=13)   # 평일 8일 + 5
    csv_text = (tmp_path / "kodex200_daily.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0] == "date,close,source,fetched_at"
    assert "2026-09-01,100.0" in csv_text
    snap1 = (tmp_path / "positions.jsonl").read_text(encoding="utf-8")
    assert len(snap1.splitlines()) == 2
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["computed_date"] == "2026-09-10"
    assert s1["windows"]["all"]["all"]["n"] == 2

    s2, _ = _update(tmp_path, T, L, _kis_rows(), now)
    assert (tmp_path / "positions.jsonl").read_text(encoding="utf-8") == snap1
    assert len((tmp_path / "summary_history.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert s1["drift"] == s2["drift"] == 0


def test_run_daily_update_drift_next_day(tmp_path):
    T, L = _simple_db()
    _update(tmp_path, T, L, _kis_rows(), datetime(2026, 9, 10, 20, 31))
    T2, L2 = _simple_db(pnl=47001)                              # DB 사후 수정 1건
    s, _ = _update(tmp_path, T2, L2, _kis_rows(), datetime(2026, 9, 11, 20, 31))
    assert s["drift"] == 1
    assert (tmp_path / "positions_prev.jsonl").is_file()
    s_again, _ = _update(tmp_path, T2, L2, _kis_rows(), datetime(2026, 9, 11, 20, 45))
    assert s_again["drift"] == 1                               # 같은 날 재실행도 같은 기준
    hist = (tmp_path / "summary_history.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["computed_date"] for x in hist] == ["2026-09-10", "2026-09-11"]


def test_partial_benchmark_response_keeps_existing_cache(tmp_path):
    T, L = _simple_db()
    old = "date,close,source,fetched_at\n" + "".join(f"{d},{c},old,x\n" for d, c in BENCH.items())
    (tmp_path / "kodex200_daily.csv").write_text(old, encoding="utf-8")
    s, _ = _update(tmp_path, T, L, _kis_rows(start="2026-09-03"), datetime(2026, 9, 10, 20, 31))
    assert (tmp_path / "kodex200_daily.csv").read_text(encoding="utf-8") == old
    assert s["windows"]["all"]["all"]["n"] == 2


def test_benchmark_at_broker_cap_is_accepted_and_older_positions_out_of_range(tmp_path, monkeypatch):
    """상한까지 요청해도 진입일에 못 미치면 부분 응답이 아니라 브로커 한계 — 받은 범위를 쓴다(coordinator 보완)."""
    monkeypatch.setattr(er, "MAX_BENCH_DAYS", 3)
    T, L = _simple_db()
    s, broker = _update(tmp_path, T, L, _kis_rows(start="2026-09-02"), datetime(2026, 9, 10, 20, 31))
    broker.get_daily_prices.assert_awaited_once_with("069500", days=3)
    assert (tmp_path / "kodex200_daily.csv").is_file()
    rows = {json.loads(x)["position_id"]: json.loads(x)
            for x in (tmp_path / "positions.jsonl").read_text(encoding="utf-8").splitlines()}
    assert rows["OK1"]["exclusion"] == "bench_out_of_range"      # 09-01 진입 < 캐시 첫 날짜 09-02
    assert rows["OK2"]["exclusion"] is None and rows["OK2"]["bench_return"] is not None


def test_broker_failure_uses_existing_cache(tmp_path):
    T, L = _simple_db()
    old = "date,close,source,fetched_at\n" + "".join(f"{d},{c},old,x\n" for d, c in BENCH.items())
    (tmp_path / "kodex200_daily.csv").write_text(old, encoding="utf-8")
    broker = SimpleNamespace(get_daily_prices=AsyncMock(side_effect=RuntimeError("KIS 장애(가짜)")))
    s, _ = _update(tmp_path, T, L, None, datetime(2026, 9, 10, 20, 31), broker=broker)
    assert (tmp_path / "kodex200_daily.csv").read_text(encoding="utf-8") == old
    rows = [json.loads(x) for x in (tmp_path / "positions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(r["bench_return"] is not None for r in rows)


def test_no_benchmark_at_all_writes_null_not_zero(tmp_path):
    T, L = _simple_db()
    _update(tmp_path, T, L, [], datetime(2026, 9, 10, 20, 31))
    rows = [json.loads(x) for x in (tmp_path / "positions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and all(r["bench_return"] is None and r["bench_missing_reason"] == "benchmark_missing" for r in rows)


def test_run_daily_update_counts_day_status(tmp_path):
    T, L = _simple_db()
    now = datetime(2026, 9, 10, 20, 31)
    s, _ = _update(tmp_path, T, L, _kis_rows(), now)
    assert s["day_status_missing"] == 2                        # 1단계: 상태 표 없음 → 전부 missing
    full = {d: "complete" for d in BENCH}
    s, _ = _update(tmp_path, T, L, _kis_rows(), now, day_status=full)
    assert s["day_status_missing"] == 0
    s, _ = _update(tmp_path, T, L, _kis_rows(), now, day_status=dict(full, **{"2026-09-02": "incomplete"}))
    assert s["windows"]["all"]["all"]["excluded"]["record_incomplete"]["n"] == 2


# ── 교차 공급자 구현 리뷰(1단계) 처분 시험 ─────────────────────────────────────

def test_cap_request_with_short_response_keeps_existing_cache(tmp_path, monkeypatch):
    """상한만큼 요청해도 받은 행이 모자라면(2페이지 실패) 부분 응답 — 기존 캐시 보존(리뷰 P1)."""
    monkeypatch.setattr(er, "MAX_BENCH_DAYS", 5)
    T, L = _simple_db()
    old = "date,close,source,fetched_at\n" + "".join(f"{d},{c},old,x\n" for d, c in BENCH.items())
    (tmp_path / "kodex200_daily.csv").write_text(old, encoding="utf-8")
    _update(tmp_path, T, L, _kis_rows(start="2026-09-08"), datetime(2026, 9, 10, 20, 31))   # 3행 < 상한 5
    assert (tmp_path / "kodex200_daily.csv").read_text(encoding="utf-8") == old


def test_empty_snapshot_same_day_rerun_keeps_drift_baseline(tmp_path):
    """비어 있지 않음 → 빈 스냅샷 → 당일 재실행 → drift 기준 유지(리뷰 P2-1)."""
    T, L = _simple_db()
    _update(tmp_path, T, L, _kis_rows(), datetime(2026, 9, 10, 20, 31))                    # 첫날 2행
    s1, _ = _update(tmp_path, [], [], _kis_rows(), datetime(2026, 9, 11, 20, 31))          # 다음 날 0행 → drift 2
    assert s1["drift"] == 2
    s2, _ = _update(tmp_path, [], [], _kis_rows(), datetime(2026, 9, 11, 21, 0))           # 같은 날 재실행
    assert s2["drift"] == 2
    s3, _ = _update(tmp_path, [], [], _kis_rows(), datetime(2026, 9, 14, 20, 31))          # 다음 계산일 — 기준은 빈 스냅샷
    assert s3["drift"] == 0


def test_history_torn_tail_is_repaired_before_append(tmp_path):
    """줄바꿈 없는 손상 꼬리 → 새 날짜 append → 재조회 → 당일 재실행(리뷰 P2-2)."""
    T, L = _simple_db()
    hist = tmp_path / "summary_history.jsonl"
    hist.write_text('{"computed_date": "2026-09-09", "n": 1}\n{"computed_date":', encoding="utf-8")
    _update(tmp_path, T, L, _kis_rows(), datetime(2026, 9, 10, 20, 31))
    _update(tmp_path, T, L, _kis_rows(), datetime(2026, 9, 10, 21, 0))
    lines = hist.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["computed_date"] for x in lines] == ["2026-09-09", "2026-09-10"]


def test_excluded_all_missing_pnl_is_null_and_line_shows_missing():
    """전부 결측인 제외 사유의 합계는 null, 한 줄에 결측 건수 표시(리뷰 P2-3)."""
    rows = [dict(schema=1, position_id="X", exclusion="exits_missing", net_pnl=None, last_exit_date="2026-09-03",
                 strategy="s", exit_quality="fill", cohort_id="c")]
    s = er.summarize(rows, [], today=date(2026, 9, 10), awaiting_close=0, day_status_missing=0)
    agg = s["windows"]["all"]["all"]["excluded"]["exits_missing"]
    assert agg == {"n": 1, "net_pnl_sum": None, "net_pnl_missing": 1}
    line = er.format_weekly_line(s)
    assert "제외 1건 -(손익 결측 1건)" in line and "+0원" not in line


def test_status_uses_bench_covered_not_n():
    """포함 30건 중 벤치마크 산출 29건이면 insufficient_sample(리뷰 시험 보강)."""
    base = dict(schema=1, exclusion=None, last_exit_date="2026-09-03", strategy="s", exit_quality="fill",
                cohort_id="c", net_return="0.01", clip_basis="common_5", entry_cost="1000", fees_total_est="1")
    rows = [dict(base, position_id=f"P{i}", excess_return="0.001", excess_krw="1", bench_return="0.009",
                 clipped_excess_return="0.001") for i in range(29)]
    rows.append(dict(base, position_id="P29", excess_return=None, excess_krw=None, bench_return=None,
                     clipped_excess_return=None))
    m = er.summarize(rows, [], today=date(2026, 9, 10), awaiting_close=0, day_status_missing=0)["windows"]["all"]["all"]
    assert m["n"] == 30 and m["bench_covered"] == 29 and m["status"] == "insufficient_sample"


# ── 2단계 T8~T10: 거래일 기록 대사·상태 표 ─────────────────────────────────────

DAY = date(2026, 9, 10)
VNOW = datetime(2026, 9, 10, 20, 31)


def _kfill(symbol, side, qty, odno="1"):
    return {"symbol": symbol, "name": "", "sll_buy_dvsn_cd": "01" if side == "SELL" else "02",
            "tot_ccld_qty": qty, "avg_prvs": 10000.0, "odno": odno, "ord_tmd": "100000"}


def _vbroker(fills=(), complete=True, reason=None, bars=None):
    return SimpleNamespace(
        get_daily_prices=AsyncMock(return_value=bars if bars is not None else _kis_rows()),
        get_fills_for_date_checked=AsyncMock(return_value=(list(fills), complete, reason)))


def _vfetch(*, sums=(), trade_rows=(), status_rows=None, status_exc=None, sums_exc=None, base=None, calls=None):
    """대사 SQL(③ 일별 합·⑤ 거래 본체·상태 표)을 먼저 가로채고 나머지는 exporter 가짜로 넘긴다."""
    base = base or _fake_fetch([], [])

    async def fetch(sql, *args):
        if calls is not None:
            calls.append((sql, args))
        if "GROUP BY e.symbol" in sql:
            if sums_exc:
                raise sums_exc
            return [dict(r) for r in sums]
        if "t.exit_quantity" in sql:
            return [dict(r) for r in trade_rows]
        if "execution_day_status" in sql:
            if status_exc:
                raise status_exc
            return [dict(r) for r in (status_rows or [])]
        return await base(sql, *args)
    return fetch


def _verify(broker, fetch, write_queue=None, day=DAY):
    return asyncio.run(er.verify_day_records(broker=broker, fetch=fetch, write_queue=write_queue, day=day))


def _sum(symbol, side, qty):
    return {"symbol": symbol, "event_type": side, "qty": D(qty)}


def test_verify_sell_match_is_complete():
    b = _vbroker([_kfill("A1", "SELL", 60, "1"), _kfill("A1", "SELL", 40, "2"), _kfill("B1", "BUY", 7)])
    f = _vfetch(sums=[_sum("A1", "SELL", 100), _sum("B1", "BUY", 3)],
                trade_rows=[{"id": "T1", "exit_quantity": 100, "sold": D(100)}])
    r = _verify(b, f)
    assert r["status"] == "complete" and r["reasons"] == []      # BUY 수량 7≠3 은 비교하지 않는다
    b.get_fills_for_date_checked.assert_awaited_once_with(DAY)


def test_verify_sell_qty_mismatch():
    r = _verify(_vbroker([_kfill("A1", "SELL", 10)]), _vfetch(sums=[_sum("A1", "SELL", 5)]))
    assert r == r | {"status": "incomplete", "reasons": ["sell_qty:A1"]}


def test_verify_sell_only_in_db():
    r = _verify(_vbroker([]), _vfetch(sums=[_sum("A1", "SELL", 5)]))
    assert r["status"] == "incomplete" and r["reasons"] == ["sell_qty:A1"]


def test_verify_buy_missing_in_db():
    r = _verify(_vbroker([_kfill("B1", "BUY", 5)]), _vfetch())
    assert r["status"] == "incomplete" and r["reasons"] == ["buy:B1"]


def test_verify_buy_only_in_db():
    r = _verify(_vbroker([]), _vfetch(sums=[_sum("B1", "BUY", 5)]))
    assert r["status"] == "incomplete" and r["reasons"] == ["buy:B1"]


def test_verify_trade_row_mismatch():
    calls = []
    f = _vfetch(sums=[_sum("A1", "SELL", 50)], calls=calls,
                trade_rows=[{"id": "T1", "exit_quantity": 100, "sold": D(100)},
                            {"id": "T2", "exit_quantity": 0, "sold": D(50)}])
    r = _verify(_vbroker([_kfill("A1", "SELL", 50)]), f)
    assert r["status"] == "incomplete" and r["reasons"] == ["trade_row:T2"]
    assert all(args == (DAY,) for sql, args in calls)            # 날짜 인자는 day(date) 하나


def test_verify_zero_zero_is_complete():
    r = _verify(_vbroker([]), _vfetch())
    assert r["status"] == "complete" and r["reasons"] == []


def test_verify_fill_query_incomplete():
    r = _verify(_vbroker([_kfill("A1", "SELL", 5)], complete=False, reason="page_failed"),
                _vfetch(sums=[_sum("A1", "SELL", 5)]))
    assert r["status"] == "incomplete" and r["reasons"] == ["fill_query_incomplete:page_failed"]


def test_verify_fill_query_unavailable():
    r = _verify(SimpleNamespace(get_daily_prices=AsyncMock()), _vfetch())
    assert r["status"] == "incomplete" and r["reasons"] == ["fill_query_unavailable"]


def test_verify_write_queue_join_timeout(monkeypatch):
    monkeypatch.setattr(er, "WRITE_QUEUE_WAIT_SEC", 0.05)

    async def run(done):
        q = asyncio.Queue()
        q.put_nowait(("INSERT …", ()))
        if done:
            q.get_nowait()
            q.task_done()
        return await er.verify_day_records(broker=_vbroker([]), fetch=_vfetch(), write_queue=q, day=DAY)

    assert asyncio.run(run(False))["reasons"] == ["write_queue_pending"]
    assert asyncio.run(run(True))["status"] == "complete"


def test_verify_db_query_exception():
    r = _verify(_vbroker([]), _vfetch(sums_exc=RuntimeError("DB 끊김(가짜)")))
    assert r["status"] == "incomplete" and r["reasons"] == ["db_query_failed"]


def test_verify_reasons_sorted_and_truncated():
    sums = [_sum(f"S{i:02d}", "SELL", 1) for i in range(25)]
    r = _verify(_vbroker([]), _vfetch(sums=list(reversed(sums))))
    assert r["reasons"][:2] == ["sell_qty:S00", "sell_qty:S01"]
    assert len(r["reasons"]) == 21 and r["reasons"][-1] == "…(+5)"


def test_save_day_status_upsert_and_failure():
    seen = []

    async def ok(sql, *args):
        seen.append((sql, args))

    async def boom(sql, *args):
        raise RuntimeError("쓰기 실패(가짜)")

    result = {"status": "incomplete", "reasons": ["buy:B1"], "checked": {}}
    assert asyncio.run(er.save_day_status(ok, DAY, result, VNOW)) is True
    sql, args = seen[0]
    assert "ON CONFLICT (trade_date) DO UPDATE" in sql and "'kr_excess_20_30'" in sql
    assert args[0] == DAY and args[1] == "incomplete" and "buy:B1" in args[2] and args[3] == VNOW
    assert asyncio.run(er.save_day_status(boom, DAY, result, VNOW)) is False


def test_load_day_status_table_missing_vs_other_failure():
    class UndefinedTableError(Exception):
        pass

    got = asyncio.run(er.load_day_status(_vfetch(status_exc=UndefinedTableError("x")), DAY))
    assert got == {}
    assert asyncio.run(er.load_day_status(
        _vfetch(status_exc=RuntimeError('relation "execution_day_status" does not exist')), DAY)) == {}
    with pytest.raises(RuntimeError):
        asyncio.run(er.load_day_status(_vfetch(status_exc=RuntimeError("연결 끊김(가짜)")), DAY))
    rows = [{"trade_date": date(2026, 9, 9), "status": "complete"}]
    assert asyncio.run(er.load_day_status(_vfetch(status_rows=rows), DAY)) == {date(2026, 9, 9): "complete"}


# run_daily_update 운영 경로(execute 주어짐)

def _today_db():
    """OK1(09-01→03) + TODAY(09-08→09-10, 오늘 청산)."""
    T, L = _simple_db()
    T = T[:1] + [_trow("TODAY", "A00003", "2026-09-08T09:05:00", exit_time="2026-09-10T14:00:00",
                       exit_price=10500, exit_qty=100, exit_type="trailing", pnl=40000)]
    L = L[:1] + [_leg("TODAY", "2026-09-10T14:00:00", 100, 10500)]
    return T, L


def _ops_update(tmp_path, broker, fetch, execute, now=VNOW):
    return asyncio.run(er.run_daily_update(
        broker=broker, fetch=fetch, out_dir=tmp_path, root=ROOT, now=now, code_sha="sha1",
        execute=execute))


def _rows_by_id(tmp_path):
    return {json.loads(x)["position_id"]: json.loads(x)
            for x in (tmp_path / "positions.jsonl").read_text(encoding="utf-8").splitlines()}


async def _noop_execute(sql, *args):
    return None


def test_ops_save_failure_applies_this_run_result(tmp_path):
    """기존 complete 행 위 저장 실패 → 이번 incomplete 가 원장에 적용·day_status_saved=false."""
    T, L = _today_db()
    all_complete = [{"trade_date": date.fromisoformat(d), "status": "complete"} for d in BENCH]
    fetch = _vfetch(sums=[_sum("A00003", "SELL", 100)], status_rows=all_complete, base=_fake_fetch(T, L))

    async def boom(sql, *args):
        raise RuntimeError("쓰기 실패(가짜)")

    s = _ops_update(tmp_path, _vbroker([]), fetch, boom)          # KIS 0 ≠ DB 100 → incomplete
    assert s["day_status_today"] == "incomplete" and s["day_status_saved"] is False
    assert s["day_status_reasons"] == ["sell_qty:A00003"]
    rows = _rows_by_id(tmp_path)
    assert rows["TODAY"]["exclusion"] == "record_incomplete"
    assert rows["OK1"]["exclusion"] is None


def test_ops_status_load_failure_aborts_and_keeps_files(tmp_path):
    T, L = _today_db()
    for name in ("positions.jsonl", "summary.json", "kodex200_daily.csv"):
        (tmp_path / name).write_text(f"old-{name}", encoding="utf-8")
    broker = _vbroker([_kfill("A00003", "SELL", 100)])
    fetch = _vfetch(sums=[_sum("A00003", "SELL", 100)], base=_fake_fetch(T, L),
                    status_exc=RuntimeError("연결 끊김(가짜)"))
    with pytest.raises(RuntimeError):
        _ops_update(tmp_path, broker, fetch, _noop_execute)
    for name in ("positions.jsonl", "summary.json", "kodex200_daily.csv"):
        assert (tmp_path / name).read_text(encoding="utf-8") == f"old-{name}"
    assert not (tmp_path / "summary_history.jsonl").exists()
    broker.get_daily_prices.assert_not_awaited()


def test_ops_status_table_missing_proceeds_without_status(tmp_path):
    class UndefinedTableError(Exception):
        pass

    T, L = _today_db()
    fetch = _vfetch(sums=[_sum("A00003", "SELL", 100)], base=_fake_fetch(T, L),
                    status_exc=UndefinedTableError('relation "execution_day_status" does not exist'))
    seen = []

    async def execute(sql, *args):
        seen.append(args)

    s = _ops_update(tmp_path, _vbroker([_kfill("A00003", "SELL", 100)]), fetch, execute)
    assert s["day_status_today"] == "complete" and s["day_status_saved"] is True
    assert seen and seen[0][0] == DAY
    # OK1(09-01~03) 은 상태 행 없음 → missing, TODAY(09-08~10) 는 09-08·09 가 없음 → missing
    assert s["day_status_missing"] == 2 and s["windows"]["all"]["all"]["n"] == 2


def test_ops_record_incomplete_and_missing_counts(tmp_path):
    T, L = _today_db()
    status = [{"trade_date": date(2026, 9, d), "status": "complete"} for d in (1, 2, 3, 8)]
    status.append({"trade_date": date(2026, 9, 9), "status": "incomplete"})
    status.append({"trade_date": DAY, "status": "incomplete"})     # 오늘 옛 판정 — 이번 결과로 덮인다
    fetch = _vfetch(sums=[_sum("A00003", "SELL", 100)], status_rows=status, base=_fake_fetch(T, L))
    s = _ops_update(tmp_path, _vbroker([_kfill("A00003", "SELL", 100)]), fetch, _noop_execute)
    assert s["day_status_today"] == "complete"
    m = s["windows"]["all"]["all"]
    assert m["excluded"]["record_incomplete"]["n"] == 1             # TODAY: 09-09 incomplete
    assert m["n"] == 1 and s["day_status_missing"] == 0            # OK1: 09-01~03 모두 complete


def test_ops_status_since_is_oldest_entry(tmp_path):
    T, L = _today_db()
    calls = []
    fetch = _vfetch(base=_fake_fetch(T, L), calls=calls, sums=[_sum("A00003", "SELL", 100)])
    _ops_update(tmp_path, _vbroker([_kfill("A00003", "SELL", 100)]), fetch, _noop_execute)
    args = [a for sql, a in calls if "execution_day_status" in sql]
    assert args == [(date(2026, 9, 1),)]


def test_without_execute_summary_has_null_day_status_fields(tmp_path):
    T, L = _simple_db()
    s, _ = _update(tmp_path, T, L, _kis_rows(), VNOW)
    assert s["day_status_today"] is None and s["day_status_reasons"] is None and s["day_status_saved"] is None


def test_day_comes_from_now_only_under_utc_and_kst(tmp_path, monkeypatch):
    """같은 now → TZ=UTC·Asia/Seoul 에서 같은 대사 날짜·같은 요약(호스트 로컬 now.date(), KST 변환 없음)."""
    import time
    results = []
    old_tz = __import__("os").environ.get("TZ")
    try:
        for tz in ("UTC", "Asia/Seoul"):
            monkeypatch.setenv("TZ", tz)
            time.tzset()
            T, L = _today_db()
            calls = []
            out = tmp_path / tz.replace("/", "_")
            out.mkdir()
            broker = _vbroker([_kfill("A00003", "SELL", 100)])
            fetch = _vfetch(sums=[_sum("A00003", "SELL", 100)], base=_fake_fetch(T, L), calls=calls)
            s = asyncio.run(er.run_daily_update(
                broker=broker, fetch=fetch, out_dir=out, root=ROOT, now=datetime(2026, 9, 10, 0, 30),
                code_sha="sha1", execute=_noop_execute))
            broker.get_fills_for_date_checked.assert_awaited_once_with(DAY)
            day_args = [a for sql, a in calls if "GROUP BY e.symbol" in sql or "t.exit_quantity" in sql]
            assert day_args and all(a == (DAY,) for a in day_args)
            results.append({k: s[k] for k in ("computed_date", "day_status_today", "day_status_missing", "rows")})
    finally:
        monkeypatch.undo()
        if old_tz is None:
            __import__("os").environ.pop("TZ", None)
        time.tzset()
    assert results[0] == results[1] and results[0]["computed_date"] == "2026-09-10"


def test_load_day_status_column_missing_is_not_table_missing():
    """열 없음 같은 다른 'does not exist' 는 표 없음으로 삼키지 않고 올린다(coordinator 보완)."""
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        asyncio.run(er.load_day_status(
            _vfetch(status_exc=RuntimeError('column "status" does not exist')), DAY))
