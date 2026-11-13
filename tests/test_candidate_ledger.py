"""V02 후보 전수 원장 — 행 구조·결측 보존·실패 시 흐름 유지·끄기 스위치."""
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


def _rows(tmp_path):
    return [json.loads(x) for x in (tmp_path / "candidates_20261113.jsonl").read_text().splitlines()]


def test_scan_row_keeps_missing_as_none_and_zero_as_zero(tmp_path):
    basis = {"source_terms": [{"source_id": "kis_volume_rank", "weighted_score": 40.0}, "잡음"],
             "merge_score_sum": 40.0, "post_merge_adjustment": 5, "occurrence_count": 2, "occurrence_bonus": 3.0,
             "pre_normalization_score": 48.0, "cache_fallback": False}
    scan_id = cl.record_scan([_stock(), _stock("000660", selection_basis=basis, reasons=None)], "regular",
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
    scan_id = cl.record_scan([], "regular", now=NOW, ledger_dir=tmp_path)
    assert cl.record_gate(scan_id, now=NOW, ledger_dir=tmp_path, gate="regime", ok=False, observed=True,
                          kospi_chg=-1.5, weighted_chg=float("inf")) is True
    assert cl.record_gate(None, now=NOW, ledger_dir=tmp_path, gate="cash", ok=True) is False
    scan, gate = _rows(tmp_path)
    assert scan["n"] == 0 and scan["candidates"] == []
    assert gate == {"kind": "gate", "scan_id": scan_id, "at": "2026-11-13T10:05:07", "gate": "regime", "ok": False,
                    "observed": True, "kospi_chg": -1.5, "weighted_chg": None}


def test_failure_and_switch_never_raise(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert cl.record_scan([_stock()], "regular", now=NOW, ledger_dir=blocker) is None       # 디렉터리 생성 실패
    assert cl.record_scan([object()], "regular", now=NOW, ledger_dir=tmp_path) is None      # 후보 속성 없음
    assert cl.record_gate("s", now=NOW, ledger_dir=blocker, gate="cash", ok=True) is False
    monkeypatch.setenv("CANDIDATE_LEDGER", "0")
    assert cl.record_scan([_stock()], "regular", now=NOW, ledger_dir=tmp_path) is None
    assert not (tmp_path / "candidates_20261113.jsonl").exists()


def test_truncates_but_reports_original_count(tmp_path):
    cl.record_scan([_stock(f"{i:06d}") for i in range(cl.MAX_CANDIDATES + 5)], "regular", now=NOW, ledger_dir=tmp_path)
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
    scan, regime, cash = _ledger_rows(tmp_path)
    assert scan["kind"] == "scan" and [c["symbol"] for c in scan["candidates"]] == ["S"] and scan["candidates"][0]["score"] == 90
    assert regime["scan_id"] == cash["scan_id"] == scan["scan_id"]
    assert (regime["gate"], regime["ok"], regime["observed"]) == ("regime", True, True)
    assert (cash["gate"], cash["ok"]) == ("cash", True) and "available_cash" not in cash


def test_real_screening_loop_records_candidates_when_regime_blocks_or_cash_is_zero(monkeypatch, tmp_path):
    from tests.test_entry_gate_scheduler import run_cycle
    _t, _c, events, _ = run_cycle(monkeypatch, ledger=cl, regime=-3)      # 약세장 차단: 후보는 남고 현금 게이트는 미도달
    _t, _c, events2, _ = run_cycle(monkeypatch, ledger=cl, cash=0)        # 현금 0: 후보는 남고 신호는 없다
    rows = _ledger_rows(tmp_path)
    assert not events and not events2 and [r["kind"] for r in rows] == ["scan", "gate", "scan", "gate", "gate"]
    assert rows[1]["gate"] == "regime" and rows[1]["ok"] is False and rows[1]["weighted_chg"] == -3
    assert rows[4]["gate"] == "cash" and rows[4]["ok"] is False
    assert all(len(r["candidates"]) == 1 for r in rows if r["kind"] == "scan")
