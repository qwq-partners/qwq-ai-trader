"""장기 구간 연구 러너의 포지션별 KODEX200 초과수익 계산 (합성 입력, 원장 정의와 같은 기준)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace as T

import pytest

_spec = importlib.util.spec_from_file_location(
    "long_window_excess", Path(__file__).resolve().parents[1] / "scripts" / "research" / "long_window_excess.py")
lwe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lwe)


def test_position_excess_partial_exits_and_clip():
    bench = {"2020-01-02": 100.0, "2020-01-03": 110.0, "2020-01-06": 90.0}
    trades = [
        T(side="BUY", symbol="A", date="2020-01-02", price=1000.0, quantity=10, amount=10000.0, fee=10.0,
          reason="", stop_pct=5.0),
        T(side="SELL", symbol="A", date="2020-01-03", price=1100.0, quantity=4, amount=4400.0, fee=20.0,
          reason="1차 익절"),
        T(side="SELL", symbol="A", date="2020-01-06", price=800.0, quantity=6, amount=4800.0, fee=30.0,
          reason="손절"),
        # 벤치 날짜 없는 포지션 → null (0 으로 채우지 않는다)
        T(side="BUY", symbol="B", date="2020-01-04", price=100.0, quantity=1, amount=100.0, fee=0.0,
          reason="", stop_pct=5.0),
        T(side="SELL", symbol="B", date="2020-01-06", price=100.0, quantity=1, amount=100.0, fee=0.0,
          reason=lwe.END_REASON),
    ]
    a, b = lwe.position_rows(trades, bench)
    net = (4400 - 20) + (4800 - 30) - 10000 - 10          # = -860
    assert a["net_pnl"] == pytest.approx(net)
    assert a["net_return"] == pytest.approx(-0.086)
    # 청산 수량 가중: 0.4×(+10%) + 0.6×(−10%) = −2%
    assert a["bench_return"] == pytest.approx(-0.02)
    assert a["excess_return"] == pytest.approx(-0.066)
    assert a["excess_krw"] == pytest.approx(net - 10000 * -0.02)
    assert a["clipped_excess_return"] == pytest.approx(-0.05 + 0.02)   # max(−8.6%, −5%) − (−2%)
    assert b["excess_return"] is None and b["bench_missing_reason"].startswith("bench_date_missing")
    assert b["forced_end"] is True
    m = lwe.metrics([a, b])
    assert m["n"] == 2 and m["bench_covered"] == 1 and m["beat_rate"] == 0


def test_regime_state_uses_prior_day_only():
    closes = {f"2020-{1 + i // 28:02d}-{1 + i % 28:02d}": 100.0 + i for i in range(260)}
    labels = lwe.regime_labels(closes)
    days = sorted(closes)
    s = lwe.state_before(labels, days[250])
    assert s == labels[days[249]] and s["ma200"] == "MA200↑" and s["vol"] == "vol≤25%"
