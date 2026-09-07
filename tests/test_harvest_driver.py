"""수확 shadow 드라이버(_process) 회귀 테스트 — 2026-09-03 리뷰 P1

구 드라이버는 최신 봉 1개만 판정해 실행이 하루 빠지면 그 봉의 손절·체결·D0가 영구 누락됐다.
합성 일봉 + 가짜 백테스트 상수만 사용 (네트워크·프로덕션 캐시 무접촉).
"""

import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.strategies import harvest_shadow as hs  # noqa: E402

BT = SimpleNamespace(FEE_RT=0.6, RISK_PCT=4.0, GAP_MAX=3.0, VALUE_GATE_MULT=2.0,
                     TRIGGER_BUF=1.003, PENDING_DAYS=3)
DAYS = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


def _bars(rows):
    """rows: [(date, open, high, low, close)] → 백테스트 prep 컬럼을 갖춘 DataFrame"""
    idx = pd.to_datetime([r[0] for r in rows])
    df = pd.DataFrame({
        "Open": [r[1] for r in rows], "High": [r[2] for r in rows],
        "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
    }, index=idx)
    df["value"] = 5e9
    df["high120"] = 90.0; df["high20"] = 95.0
    df["val_avg20"] = 4e9; df["val_med20"] = 1e9
    df["ret20"] = 0.1; df["ma20"] = 80.0; df["ma60"] = 70.0
    df["low10"] = 50.0; df["low20"] = 50.0
    return df


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(hs, "_DIR", tmp_path)
    monkeypatch.setattr(hs, "_LEDGER", tmp_path / "ledger.jsonl")


def test_missed_bar_stop_is_still_applied(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    d = _bars([(DAYS[0], 100, 101, 99, 100), (DAYS[1], 100, 101, 99, 100),
               (DAYS[2], 99, 100, 95, 99),      # ← 놓친 봉: Low 95 ≤ stop 96
               (DAYS[3], 100, 102, 99, 101)])   # 최신 봉만 보면 손절 없음
    positions = {"A": {"entry": 100.0, "stop": 96.0, "entry_date": DAYS[1], "runner": False}}
    events, new_d0, cursor = hs._process(BT, {"A": d}, set(), {"A"}, {}, positions,
                                         {"last_bar": DAYS[1]})
    assert positions == {} and any("손절" in e for e in events)
    rec = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[0])
    assert rec["exit"] == 96.0 and rec["exit_date"] == DAYS[2]
    assert cursor["last_bar"] == DAYS[3]


def test_pending_fills_on_skipped_bar_then_position_walks_forward(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    d = _bars([(DAYS[0], 100, 100, 100, 100),
               (DAYS[1], 101, 104, 100, 103),    # 체결 봉: High 104 ≥ trigger 103
               (DAYS[2], 104, 106, 103, 105)])
    pending = {"A": {"detected_date": DAYS[0], "d0_close": 100.0, "trigger": 103.0,
                     "val_med20": 1e9, "status": "waiting", "attempts": []}}
    positions = {}
    events, _, cursor = hs._process(BT, {"A": d}, set(), {"A"}, pending, positions,
                                    {"last_bar": DAYS[0]})
    assert "A" not in pending and positions["A"]["entry"] == 103.0
    assert positions["A"]["entry_date"] == DAYS[1]
    assert positions["A"]["last_bar"] == DAYS[2]   # 체결 이후 봉도 같은 실행에서 판정됨
    assert any(e.startswith("진입") for e in events)


def test_pending_expires_after_pending_days(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    d = _bars([(DAYS[i], 100, 100, 99, 100) for i in range(4)])  # 트리거 미도달
    pending = {"A": {"detected_date": DAYS[0], "d0_close": 100.0, "trigger": 103.0,
                     "val_med20": 1e9, "status": "waiting", "attempts": []}}
    events, _, _ = hs._process(BT, {"A": d}, set(), {"A"}, pending, {}, {"last_bar": DAYS[0]})
    assert "A" not in pending and any("만료" in e for e in events)


def test_first_run_without_cursor_evaluates_latest_bar_only(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    d = _bars([(DAYS[0], 100, 101, 99, 100), (DAYS[1], 99, 100, 95, 99),  # 과거 봉의 손절
               (DAYS[2], 100, 102, 99, 101)])
    positions = {"A": {"entry": 100.0, "stop": 96.0, "entry_date": DAYS[0], "runner": False}}
    events, _, cursor = hs._process(BT, {"A": d}, set(), {"A"}, {}, positions, {})
    assert "A" in positions and events == []      # 승급된 stop 을 과거 봉에 재적용하지 않는다
    assert positions["A"]["last_bar"] == DAYS[2] and cursor["last_bar"] == DAYS[2]


def test_d0_detection_and_resignal_suppression(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    d = _bars([(DAYS[0], 100, 100, 99, 100), (DAYS[1], 3000, 3100, 2990, 3050)])
    d.loc[d.index[-1], "value"] = 2e10  # ≥ 3×val_avg20
    ok = {DAYS[1]}
    pending = {}
    _, new_d0, cursor = hs._process(BT, {"A": d}, ok, {"A"}, pending, {}, {"last_bar": DAYS[0]})
    assert new_d0 == 1 and pending["A"]["detected_date"] == DAYS[1]
    assert cursor["last_d0"]["A"] == DAYS[1]
    # 10봉 이내 재신호는 억제 (백테스트 find_d0_signals 동일)
    pending2 = {}
    _, new_d0_2, _ = hs._process(BT, {"A": d}, ok, {"A"}, pending2, {},
                                 {"last_bar": DAYS[0], "last_d0": {"A": DAYS[0]}})
    assert new_d0_2 == 0 and pending2 == {}
    # 유니버스 밖 종목은 D0 감지 안 함 (보유·대기 판정은 위 테스트처럼 유지)
    _, new_d0_3, _ = hs._process(BT, {"A": d}, ok, set(), {}, {}, {"last_bar": DAYS[0]})
    assert new_d0_3 == 0


def test_drop_incomplete_bar_removes_today():
    d = _bars([(DAYS[0], 1, 1, 1, 1), (DAYS[1], 1, 1, 1, 1)])
    out = hs._drop_incomplete_bar(d, date(2026, 9, 1))
    assert list(out.index.strftime("%Y-%m-%d")) == [DAYS[0]]
    assert hs._drop_incomplete_bar(None, date(2026, 9, 1)) is None
