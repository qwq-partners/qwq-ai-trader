"""V02 후보 전수 원장 — 행 구조·결측 보존·실패 시 흐름 유지·끄기 스위치."""
import asyncio
import json
import os
import stat
from datetime import datetime
from types import SimpleNamespace

from src.analytics import candidate_ledger as cl

NOW = datetime(2026, 11, 13, 10, 5, 7)


def _stock(symbol="005930", **kw):
    base = dict(symbol=symbol, score=82.5, price=71000, change_pct=3.2, volume=1000, volume_ratio=2.5, rsi=None,
                atr_pct=float("nan"), per=0, pbr=None, has_foreign_buying=True, has_inst_buying=False,
                reasons=["거래량 급증", "신고가"], selection_basis=None)
    return SimpleNamespace(**{**base, **kw})


def scan(*a, **kw):
    return asyncio.run(cl.record_scan(*a, **kw))


def gate(*a, **kw):
    return asyncio.run(cl.record_gate(*a, **kw))


def _rows(tmp_path):
    return [json.loads(x) for x in (tmp_path / "candidates_20261113.jsonl").read_text().splitlines()]


def test_scan_row_keeps_missing_as_none_and_zero_as_zero(tmp_path):
    basis = {"source_terms": [{"source_id": "kis_volume_rank", "weighted_score": 40.0}, "잡음"],
             "merge_score_sum": 40.0, "post_merge_adjustment": 5, "occurrence_count": 2, "occurrence_bonus": 3.0,
             "pre_normalization_score": 48.0, "cache_fallback": False}
    scan_id = scan([_stock(), _stock("000660", selection_basis=basis, reasons=None)], "regular",
                             now=NOW, ledger_dir=tmp_path)
    (row,) = _rows(tmp_path)
    assert row["kind"] == "scan" and row["scan_id"] == scan_id and row["n"] == 2 and row["at"] == "2026-11-13T10:05:07"
    a, b = row["candidates"]
    assert a["rsi"] is None and a["atr_pct"] is None and a["pbr"] is None   # NaN·None 은 미관측
    assert a["per"] == 0 and a["foreign"] is True and a["inst"] is False     # 0·False 는 관측값
    assert "basis" not in a and b["reasons"] == []
    assert b["basis"] == {"terms": [["kis_volume_rank", 40.0]], "merge": 40.0, "adj": 5, "occ": 2, "bonus": 3.0,
                          "pre": 48.0, "cache": False}
    assert stat.S_IMODE(os.stat(tmp_path / "candidates_20261113.jsonl").st_mode) == 0o600


def test_gate_rows_append_and_skip_without_scan(tmp_path):
    scan_id = scan([], "regular", now=NOW, ledger_dir=tmp_path)
    assert gate(scan_id, now=NOW, ledger_dir=tmp_path, gate="cash", ok=False, extra=float("inf")) is True
    assert gate(None, now=NOW, ledger_dir=tmp_path, gate="cash", ok=True) is False
    first, row = _rows(tmp_path)
    assert first["n"] == 0 and first["candidates"] == []
    assert row == {"kind": "gate", "scan_id": scan_id, "at": "2026-11-13T10:05:07", "gate": "cash", "ok": False,
                   "extra": None}


def test_regime_row_records_only_observed_changes(tmp_path):
    """조회 실패는 null 이다 — 정책이 쓰는 0.0 대체값을 원장에 적지 않는다. 한쪽만 실패하면 다른 쪽은 남긴다."""
    regime = lambda *a: asyncio.run(cl.record_regime("s", *a, now=NOW, ledger_dir=tmp_path))   # noqa: E731
    assert regime(True, {"change_pct": 0.0}, {"change_pct": -0.4})
    assert regime(True, RuntimeError("조회 실패"), None)
    assert regime(False, {"change_pct": -3.1}, {"change_pct": float("nan")})
    assert regime(True, {"change_pct": False}, {})
    rows = [(r["ok"], r["observed"], r["kospi_chg"], r["kosdaq_chg"]) for r in _rows(tmp_path)]
    assert rows == [(True, True, 0.0, -0.4), (True, False, None, None), (False, False, -3.1, None),
                    (True, False, None, None)]
    assert all("weighted_chg" not in r for r in _rows(tmp_path))


def test_short_write_continues_and_stalled_write_fails(tmp_path, monkeypatch):
    real = os.write
    monkeypatch.setattr(cl.os, "write", lambda fd, data: real(fd, bytes(data[:7])))   # 7바이트씩만 써진다
    assert scan([_stock()], "regular", now=NOW, ledger_dir=tmp_path) is not None
    assert _rows(tmp_path)[0]["candidates"][0]["symbol"] == "005930"                   # 잘리지 않은 한 줄
    monkeypatch.setattr(cl.os, "write", lambda fd, data: 0)                            # 진전 없음 → 실패(성공으로 치지 않는다)
    assert scan([_stock()], "regular", now=NOW, ledger_dir=tmp_path) is None
    assert len(_rows(tmp_path)) == 1


def test_slow_disk_does_not_block_event_loop_and_gives_up(tmp_path, monkeypatch):
    """디스크가 멈춰도 이벤트 루프의 다른 태스크는 돌고, 시한 뒤 그 행만 포기한다."""
    import threading
    release = threading.Event()
    monkeypatch.setattr(cl, "_append", lambda *a: release.wait(5))
    monkeypatch.setattr(cl, "WRITE_TIMEOUT_SEC", 0.2)

    async def main():
        ticks = 0

        async def other():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)
        task = asyncio.create_task(other())
        result = await cl.record_scan([_stock()], "regular", now=NOW, ledger_dir=tmp_path)
        task.cancel()
        return result, ticks
    try:
        result, ticks = asyncio.run(main())
    finally:
        release.set()
    assert result is None and ticks >= 5


def test_failure_and_switch_never_raise(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert scan([_stock()], "regular", now=NOW, ledger_dir=blocker) is None       # 디렉터리 생성 실패
    assert scan([object()], "regular", now=NOW, ledger_dir=tmp_path) is None      # 후보 속성 없음
    assert gate("s", now=NOW, ledger_dir=blocker, gate="cash", ok=True) is False
    monkeypatch.setenv("CANDIDATE_LEDGER", "0")
    assert scan([_stock()], "regular", now=NOW, ledger_dir=tmp_path) is None
    assert not (tmp_path / "candidates_20261113.jsonl").exists()


def test_truncates_but_reports_original_count(tmp_path):
    scan([_stock(f"{i:06d}") for i in range(cl.MAX_CANDIDATES + 5)], "regular", now=NOW, ledger_dir=tmp_path)
    (row,) = _rows(tmp_path)
    assert row["n"] == cl.MAX_CANDIDATES + 5 and len(row["candidates"]) == cl.MAX_CANDIDATES


def _ledger_rows(tmp_path):
    files = list((tmp_path / "candidate-ledger").glob("candidates_*.jsonl"))
    return [json.loads(x) for f in files for x in f.read_text().splitlines()]


def test_real_screening_loop_records_before_gates_without_changing_flow(monkeypatch, tmp_path):
    """실제 run_screening: 원장이 켜지면 후보·게이트가 남고, 신호·호출·게이트 추적은 꺼졌을 때와 같다."""
    from tests.test_entry_gate_scheduler import run_cycle
    trace0, calls0, events0, _ = run_cycle(monkeypatch)
    trace1, calls1, events1, _ = run_cycle(monkeypatch, ledger=cl)
    kwargs0 = next(a[1] for a in calls0 if a[0] == "screen")
    kwargs1 = next(a[1] for a in calls1 if a[0] == "screen")
    assert "capture_selection" not in kwargs0 and kwargs1.get("capture_selection") is True   # 점수 구성 기록만 켠다
    same = lambda t: [(m, a[:1] if m == "signal" else a, kw) for m, a, kw in t.calls]   # noqa: E731 — 신호 ID 는 매번 새로 생긴다
    assert len(events1) == len(events0) == 1 and same(trace1) == same(trace0)
    first, regime, cash = _ledger_rows(tmp_path)
    assert first["kind"] == "scan" and [c["symbol"] for c in first["candidates"]] == ["S"] and first["candidates"][0]["score"] == 90
    assert regime["scan_id"] == cash["scan_id"] == first["scan_id"]
    assert (regime["gate"], regime["ok"], regime["observed"], regime["kospi_chg"]) == ("regime", True, True, 0)
    assert (cash["gate"], cash["ok"]) == ("cash", True) and "available_cash" not in cash


def test_real_screening_loop_records_candidates_when_regime_blocks_or_cash_is_zero(monkeypatch, tmp_path):
    from tests.test_entry_gate_scheduler import run_cycle
    _t, _c, events, _ = run_cycle(monkeypatch, ledger=cl, regime=-3)      # 약세장 차단: 후보는 남고 현금 게이트는 미도달
    _t, _c, events2, _ = run_cycle(monkeypatch, ledger=cl, cash=0)        # 현금 0: 후보는 남고 신호는 없다
    rows = _ledger_rows(tmp_path)
    assert not events and not events2 and [r["kind"] for r in rows] == ["scan", "gate", "scan", "gate", "gate"]
    assert rows[1]["gate"] == "regime" and rows[1]["ok"] is False and rows[1]["kospi_chg"] == rows[1]["kosdaq_chg"] == -3
    assert rows[4]["gate"] == "cash" and rows[4]["ok"] is False
    assert all(len(r["candidates"]) == 1 for r in rows if r["kind"] == "scan")


def test_real_screening_loop_regime_quote_failure_is_null_not_zero(monkeypatch, tmp_path):
    """지수 조회가 실패하면 정책은 종전대로 0.0 대체값으로 통과시키지만 원장에는 미관측(null)으로 남는다."""
    from tests.test_entry_gate_scheduler import run_cycle
    _t, _c, events, _ = run_cycle(monkeypatch, ledger=cl, regime=RuntimeError("시세 실패"))
    _t, _c, events0, _ = run_cycle(monkeypatch, regime=RuntimeError("시세 실패"))
    regime = next(r for r in _ledger_rows(tmp_path) if r.get("gate") == "regime")
    assert len(events) == len(events0)   # 정책 동작은 원장 유무와 무관
    assert (regime["ok"], regime["observed"], regime["kospi_chg"], regime["kosdaq_chg"]) == (True, False, None, None)
