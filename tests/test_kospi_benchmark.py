"""KOSPI 일봉 공용 로더 — 원천 순서·신선도·소비처 실패 동작 (2026-09-28).

09-28 진단: FDR "KS11" 은 09-17 장중 부분봉에서 멈춘 채 예외 없이 오래된 프레임을 줬고,
"YAHOO:^KS11" 은 직전 거래일(09-23, 추석 연휴 앞)까지 최신이었다. 합성 자료만 사용.
"""

import asyncio
import json
from datetime import date, datetime

import pandas as pd
import pytest

from src.strategies import harvest_shadow as hs
from src.utils import kospi_benchmark as kb
from src.utils import volatility_targeting as vt
from src.utils.session import KST

NOW = datetime(2026, 9, 28, 8, 30)   # 추석 연휴(09-24~26) 뒤 첫 거래일 장전


def frame(last, values=None):
    values = values if values is not None else [100.0 + i for i in range(60)]
    return pd.DataFrame({"Close": values}, index=pd.bdate_range(end=last, periods=len(values)))


def stub(**by_symbol):
    calls = []

    def fetch(symbol, start):
        calls.append(symbol)
        value = by_symbol[symbol]
        if isinstance(value, Exception):
            raise value
        return value
    return fetch, calls


def test_chuseok_gap_uses_fresh_yahoo_without_reading_ks11():
    fetch, calls = stub(**{"YAHOO:^KS11": frame("2026-09-23"), "KS11": frame("2026-09-17")})
    closes, status = kb.load_kospi_daily("2025-09-01", NOW, fetch=fetch)
    assert calls == ["YAHOO:^KS11"]
    assert status["status"] == "fresh" and status["source"] == "FDR:YAHOO:^KS11"
    assert status["last_bar_date"] == date(2026, 9, 23)
    assert closes.index[-1] == pd.Timestamp("2026-09-23") and closes.iloc[-1] == 159.0


def test_stale_ks11_frame_is_rejected_when_yahoo_is_missing():
    fetch, calls = stub(**{"YAHOO:^KS11": None, "KS11": frame("2026-09-17")})
    closes, status = kb.load_kospi_daily("2025-09-01", NOW, fetch=fetch)
    assert calls == ["YAHOO:^KS11", "KS11"]
    assert closes is None
    assert (status["status"], status["source"]) == ("stale", "FDR:KS11")


def test_stale_evidence_is_not_overwritten_by_later_missing():
    fetch, _ = stub(**{"YAHOO:^KS11": frame("2026-09-17"), "KS11": RuntimeError("합성 장애")})
    closes, status = kb.load_kospi_daily("2025-09-01", NOW, fetch=fetch)
    assert closes is None
    assert (status["status"], status["source"], status["reason"]) == (
        "stale", "FDR:YAHOO:^KS11", "older_than_previous_kr_session")


def test_mid_history_nan_close_is_invalid_not_dropped():
    data = frame("2026-09-23")
    data.iloc[30, 0] = float("nan")
    fetch, _ = stub(**{"YAHOO:^KS11": data, "KS11": None})
    closes, status = kb.load_kospi_daily("2025-09-01", NOW, fetch=fetch)
    assert closes is None
    assert (status["status"], status["reason"]) == ("unknown", "invalid_close_history")


def test_aware_now_is_judged_in_kst():
    fetch, _ = stub(**{"YAHOO:^KS11": frame("2026-09-23"), "KS11": None})
    closes, status = kb.load_kospi_daily("2025-09-01", NOW.replace(tzinfo=KST), fetch=fetch)
    assert status["status"] == "fresh" and status["loaded_at"].tzinfo is None


# ── 변동성 타게팅 ────────────────────────────────────────────


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.replace(tzinfo=KST).astimezone(tz) if tz is not None else NOW


@pytest.fixture
def vol_env(monkeypatch, tmp_path):
    cache = tmp_path / "vol_targeting.json"
    monkeypatch.setattr(vt, "_CACHE_FILE", cache)
    monkeypatch.setattr(vt, "datetime", _Clock)
    monkeypatch.setenv("VOL_TARGETING", "1")
    vt._mem_cache.clear()
    yield cache
    vt._mem_cache.clear()


def test_vol_refresh_skips_and_keeps_cache_when_no_source_is_fresh(monkeypatch, vol_env):
    fetch, calls = stub(**{"YAHOO:^KS11": frame("2026-09-17"), "KS11": frame("2026-09-17"),
                           "069500": None})
    monkeypatch.setattr(kb, "_fdr_fetch", fetch)
    assert asyncio.run(vt.refresh_vol_state()) is False
    assert calls == ["YAHOO:^KS11", "KS11", "069500"]
    assert not vol_env.exists() and vt._mem_cache == {}


def test_vol_refresh_records_bar_date_source_and_mult(monkeypatch, vol_env):
    zigzag = [100.0 if i % 2 == 0 else 103.0 for i in range(80)]   # 일 ±3% → 고변동
    fetch, calls = stub(**{"YAHOO:^KS11": frame("2026-09-23", zigzag), "KS11": None,
                           "069500": None})
    monkeypatch.setattr(kb, "_fdr_fetch", fetch)
    assert asyncio.run(vt.refresh_vol_state()) is True
    assert calls == ["YAHOO:^KS11"]
    state = json.loads(vol_env.read_text())
    assert state["last_bar_date"] == "2026-09-23" and state["source"] == "FDR:YAHOO:^KS11"
    assert state["realized_vol"] > vt.VOL_THRESHOLD
    assert vt.MIN_MULT < state["mult"] < 1.0
    assert state["mult"] == pytest.approx(vt.VOL_TARGET / state["realized_vol"], abs=2e-3)


def test_vol_refresh_069500_is_last_resort_with_same_validation(monkeypatch, vol_env):
    fetch, calls = stub(**{"YAHOO:^KS11": None, "KS11": frame("2026-09-17"),
                           "069500": frame("2026-09-23")})
    monkeypatch.setattr(kb, "_fdr_fetch", fetch)
    assert asyncio.run(vt.refresh_vol_state()) is True
    assert calls == ["YAHOO:^KS11", "KS11", "069500"]
    assert json.loads(vol_env.read_text())["source"] == "FDR:069500"


# ── 수확 shadow ─────────────────────────────────────────────


def test_harvest_regime_rule_matches_backtest():
    closes = pd.Series([10.0] * 20 + [11.0, 9.0], index=pd.bdate_range("2026-08-03", periods=22))
    # 백테스트 규칙: 종가 > 20일 단순이동평균 (창이 차기 전 NaN 은 제외)
    assert hs.regime_ok_dates(closes) == {str(closes.index[20])[:10]}


def nan_last(last):
    """09-29 재현: FDR "YAHOO:^KS11" 이 빠진 거래일 행을 NaN 종가로 돌려준다 → 이력 전체 거부."""
    data = frame(last)
    data.iloc[-1, 0] = float("nan")
    return data


@pytest.fixture
def harvest_env(monkeypatch, tmp_path):
    for name, attr in (("pending.json", "_PENDING"), ("positions.json", "_POSITIONS"),
                       ("cursor.json", "_CURSOR")):
        monkeypatch.setattr(hs, attr, tmp_path / name)
    monkeypatch.setattr(hs, "_DIR", tmp_path)
    monkeypatch.setattr(hs, "datetime", _Clock)
    monkeypatch.setattr(hs, "_load_bt", lambda: object())
    logs = []
    monkeypatch.setattr(hs.logger, "info", lambda msg: logs.append(("INFO", msg)))
    monkeypatch.setattr(hs.logger, "warning", lambda msg: logs.append(("WARNING", msg)))
    return tmp_path, logs


def test_harvest_skips_run_and_keeps_cursor_when_kospi_is_not_fresh(monkeypatch, harvest_env):
    tmp_path, _ = harvest_env
    cursor = tmp_path / "cursor.json"
    cursor.write_text('{"last_bar": "2026-09-17", "last_d0": {}}', encoding="utf-8")
    fetch, calls = stub(**{"YAHOO:^KS11": nan_last("2026-09-23"), "KS11": frame("2026-09-17"),
                           "069500": None})
    monkeypatch.setattr(kb, "_fdr_fetch", fetch)

    def _never(*args, **kwargs):
        raise AssertionError("신선하지 않은 체제 게이트로 판정하면 안 된다")
    monkeypatch.setattr(hs, "_process", _never)
    monkeypatch.setattr(hs, "_load_universe", _never)

    ok, message = asyncio.run(hs.run_daily_shadow_scan())
    assert calls == ["YAHOO:^KS11", "KS11", "069500"]
    assert ok is False and "신선하지 않음" in message   # 스케줄러가 dedup 없이 재시도
    assert "source=FDR:KS11" in message and "status=stale" in message   # 먼저 확인한 근거 보존
    assert cursor.read_text(encoding="utf-8") == '{"last_bar": "2026-09-17", "last_d0": {}}'
    assert not (tmp_path / "pending.json").exists()


KODEX = [30000.0 + 50 * i + 300 * (-1) ** i for i in range(60)]   # 원 단위 대용 종가, 일 ±2% 안팎


def shifted(values, at, factor=1.242):
    """at 번째 봉부터 수준을 올린다 → at 에만 +24.2% 급 일수익률 1건 (09-28 이전 FDR 069500 오염 실측 크기)."""
    return [v * factor if i >= at else v for i, v in enumerate(values)]


@pytest.mark.parametrize("at,factor,expected", [
    (59, 1.242, "+24.2%"), (30, 1.242, "+24.2%"), (29, 1.242, None), (1, 1.242, None),
    (40, 0.758, "-24.2%"),   # 음의 오염도 거부 (abs 검사)
])
def test_proxy_outlier_checks_only_last_lookback_returns(at, factor, expected):
    values = shifted([100.0] * 60, at, factor)
    dates = list(pd.bdate_range(end="2026-09-23", periods=60).date)
    found = kb.proxy_outlier(values, dates, 30)   # 끝 30개 수익률 = 인덱스 30..59
    assert found == (None if expected is None else f"{dates[at]} {expected}")


@pytest.mark.parametrize("case", ["kodex_fallback", "yahoo_fresh"])
def test_harvest_regime_gate_uses_fallback_only_when_kospi_sources_fail(
        monkeypatch, harvest_env, case):
    tmp_path, logs = harvest_env
    (tmp_path / "cursor.json").write_text('{"last_bar": "2026-09-17", "last_d0": {}}', encoding="utf-8")
    if case == "kodex_fallback":   # 2026-09-29 운영 상태: Yahoo NaN 행 + KS11 정지 (커서 09-23 보유)
        by_symbol = {"YAHOO:^KS11": nan_last("2026-09-23"), "KS11": frame("2026-09-17"),
                     "069500": frame("2026-09-23", KODEX)}
        chosen, source, level = "069500", "FDR:069500", "WARNING"
    else:   # Yahoo 는 대용 이상치 검사 대상이 아니다 — 창 안 급등 봉이 있어도 채택
        by_symbol = {"YAHOO:^KS11": frame("2026-09-23", shifted([100.0 + i for i in range(60)], 55)),
                     "KS11": frame("2026-09-17"),
                     "069500": AssertionError("KOSPI 가 신선하면 069500 을 조회하지 않는다")}
        chosen, source, level = "YAHOO:^KS11", "FDR:YAHOO:^KS11", "INFO"
    fetch, calls = stub(**by_symbol)
    monkeypatch.setattr(kb, "_fdr_fetch", fetch)
    monkeypatch.setattr(hs, "_load_universe", lambda bt: [])
    seen = {}

    def _process(bt, data, ok_dates, universe, pending, positions, cursor):
        seen["ok_dates"] = ok_dates
        return [], 0, cursor
    monkeypatch.setattr(hs, "_process", _process)

    ok, _ = asyncio.run(hs.run_daily_shadow_scan())
    assert ok is True
    assert calls[-1] == chosen and "069500" not in calls[:-1]
    closes = by_symbol[chosen]["Close"]
    assert seen["ok_dates"] == hs.regime_ok_dates(closes) and seen["ok_dates"]
    gate = [(lv, msg) for lv, msg in logs if "체제 게이트 원천" in msg]
    assert gate == [(level, gate[0][1])] and source in gate[0][1]
    assert ("KOSPI 대용(KODEX200)" in gate[0][1]) is (level == "WARNING")


# ── 백테스트·분석용 과거 구간 로더 (scripts/ 벤치마크 교체, 2026-09-28) ──────────

def range_stub(**by_symbol):
    calls = []

    def fetch(symbol, start, end):
        calls.append((symbol, start, end))
        value = by_symbol[symbol]
        if isinstance(value, Exception):
            raise value
        return value
    return fetch, calls


def test_history_prefers_yahoo_and_includes_end_day():
    fetch, calls = range_stub(**{"YAHOO:^KS11": frame("2026-09-23"), "KS11": frame("2026-09-17")})
    df, source = kb.load_kospi_history("2026-06-01", "2026-09-23", fetch=fetch)
    assert source == "FDR:YAHOO:^KS11" and df.index[-1].date() == date(2026, 9, 23)
    assert calls == [("YAHOO:^KS11", "2026-06-01", "2026-09-24")]  # Yahoo end 배타 → 하루 더


def test_history_falls_back_to_ks11_on_error_or_empty():
    fetch, calls = range_stub(**{"YAHOO:^KS11": RuntimeError("차단"), "KS11": frame("2026-09-17")})
    df, source = kb.load_kospi_history("2026-06-01", "2026-09-17", fetch=fetch)
    assert source == "FDR:KS11" and calls[-1] == ("KS11", "2026-06-01", "2026-09-17")  # KS11 end 는 포함 그대로
    fetch, _ = range_stub(**{"YAHOO:^KS11": pd.DataFrame(), "KS11": pd.DataFrame()})
    assert kb.load_kospi_history("2026-06-01", fetch=fetch) == (None, None)


def test_history_warns_only_when_open_ended_series_is_frozen(monkeypatch):
    warnings = []
    monkeypatch.setattr(kb.logger, "warning", lambda msg: warnings.append(msg))
    fetch, _ = range_stub(**{"YAHOO:^KS11": frame("2026-09-17"), "KS11": frame("2026-09-17")})
    df, source = kb.load_kospi_history("2026-06-01", fetch=fetch, now=NOW)  # end 없음 = 오늘까지
    assert source == "FDR:YAHOO:^KS11"
    assert any("직전 거래일보다 오래됨" in w for w in warnings)
    warnings.clear()
    kb.load_kospi_history("2026-06-01", "2026-09-17", fetch=fetch, now=NOW)  # 과거 구간 명시 → 경고 없음
    fresh, _ = range_stub(**{"YAHOO:^KS11": frame("2026-09-23"), "KS11": frame("2026-09-17")})
    kb.load_kospi_history("2026-06-01", fetch=fresh, now=NOW)  # 추석 뒤 직전 거래일 봉 → 정상
    assert warnings == []


def test_history_trims_rows_after_end_regardless_of_timezone():
    # FDR Yahoo 는 로컬 자정 기준이라 UTC 에선 end 다음 거래일이 섞인다 — 잘라서 end 포함으로 고정
    fetch, _ = range_stub(**{"YAHOO:^KS11": frame("2026-09-25"), "KS11": frame("2026-09-17")})
    df, _ = kb.load_kospi_history("2026-06-01", "2026-09-23", fetch=fetch)
    assert df.index[-1].date() == date(2026, 9, 23)


def test_history_trim_handles_timezone_aware_index_and_empty_result():
    aware = frame("2026-09-25")
    aware.index = aware.index.tz_localize("UTC")          # 00:00 UTC = 같은 날 09:00 KST
    fetch, _ = range_stub(**{"YAHOO:^KS11": aware, "KS11": frame("2026-09-17")})
    df, source = kb.load_kospi_history("2026-06-01", "20260923", fetch=fetch)
    assert source == "FDR:YAHOO:^KS11" and df.index[-1].date() == date(2026, 9, 23)
    late, _ = range_stub(**{"YAHOO:^KS11": frame("2026-09-25").loc["2026-09-24":],
                            "KS11": frame("2026-09-17")})
    df, source = kb.load_kospi_history("2026-06-01", "2026-09-23", fetch=late)   # 절단 뒤 빈 결과 → 다음 원천
    assert source == "FDR:KS11"


def test_history_skips_source_with_nan_or_nonpositive_close(monkeypatch):
    """반환 구간 종가에 NaN/inf/0 이하가 있으면 그 원천을 건너뛴다 — dropna·보간 없이 (2026-09-29)."""
    warnings = []
    monkeypatch.setattr(kb.logger, "warning", lambda msg: warnings.append(msg))
    nan = frame("2026-09-28")
    nan.iloc[-1, 0] = float("nan")                         # 09-29 Yahoo ^KS11 의 09-28 NaN 행
    fetch, _ = range_stub(**{"YAHOO:^KS11": nan, "KS11": frame("2026-09-28")})
    df, source = kb.load_kospi_history("2026-06-01", "2026-09-28", fetch=fetch)
    assert source == "FDR:KS11" and df["Close"].notna().all() and len(df) == 60
    assert any("YAHOO:^KS11" in w and "원천 제외" in w for w in warnings)
    zero = frame("2026-09-28")
    zero.iloc[5, 0] = 0.0
    fetch, _ = range_stub(**{"YAHOO:^KS11": nan, "KS11": zero})
    assert kb.load_kospi_history("2026-06-01", "2026-09-28", fetch=fetch) == (None, None)
    inf = frame("2026-09-28")
    inf.iloc[3, 0] = float("inf")
    fetch, _ = range_stub(**{"YAHOO:^KS11": inf, "KS11": frame("2026-09-28")})
    assert kb.load_kospi_history("2026-06-01", "2026-09-28", fetch=fetch)[1] == "FDR:KS11"
    neg = frame("2026-09-28")
    neg.iloc[7, 0] = -1.0                                  # 음수 종가도 '0 이하' — '== 0' 으로 좁히면 채택된다
    fetch, _ = range_stub(**{"YAHOO:^KS11": neg, "KS11": frame("2026-09-28")})
    assert kb.load_kospi_history("2026-06-01", "2026-09-28", fetch=fetch)[1] == "FDR:KS11"
    fetch, _ = range_stub(**{"YAHOO:^KS11": frame("2026-09-28"), "KS11": nan})
    assert kb.load_kospi_history("2026-06-01", "2026-09-28", fetch=fetch)[1] == "FDR:YAHOO:^KS11"


def test_history_checks_close_after_end_trim():
    tail = frame("2026-09-29")
    tail.iloc[-1, 0] = float("nan")                        # end 뒤 NaN 행은 잘린 뒤 검사 → 채택
    fetch, _ = range_stub(**{"YAHOO:^KS11": tail, "KS11": frame("2026-09-17")})
    assert kb.load_kospi_history("2026-06-01", "2026-09-28", fetch=fetch)[1] == "FDR:YAHOO:^KS11"


def test_backtest_scripts_do_not_read_frozen_fdr_ks11_directly():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for name in ("ab_exit_policy.py", "backtest_strategies.py", "backtest_t1_gate.py", "quick_backtest.py"):
        text = (root / "scripts" / name).read_text(encoding="utf-8")
        assert 'DataReader("KS11"' not in text, name
        # quick_backtest 는 연구 venv(loguru 없음)라 src 로더 대신 Yahoo 기호를 직접 쓴다
        assert ('DataReader("YAHOO:^KS11"' in text) if name == "quick_backtest.py" else ("load_kospi_history" in text), name


def _load_script(name, alias):
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_t1_gate_regime_dates_come_from_shared_history_loader(monkeypatch):
    closes = [100.0] * 25 + [110.0] * 5          # 마지막 5일만 20일선 위
    fake = pd.DataFrame({"Close": closes}, index=pd.bdate_range(end="2026-09-23", periods=len(closes)))
    monkeypatch.setattr(kb, "load_kospi_history", lambda start, end=None: (fake, "FDR:YAHOO:^KS11"))
    gate = _load_script("backtest_t1_gate.py", "_t1_gate_for_bench_test")
    ok = gate._regime_ok_dates("2026-06-01")
    assert ok == {str(d)[:10] for d in fake.index[-5:]}
    monkeypatch.setattr(kb, "load_kospi_history", lambda start, end=None: (None, None))
    with pytest.raises(RuntimeError):
        gate._regime_ok_dates("2026-06-01")


def test_ab_exit_policy_benchmark_falls_back_to_shared_loader(monkeypatch):
    import FinanceDataReader as fdr

    def _no_kodex(code, start, end=None):
        raise ConnectionError("차단")
    monkeypatch.setattr(fdr, "DataReader", _no_kodex)
    fake = pd.DataFrame({"Close": [100.0, 110.0]}, index=pd.bdate_range(end="2026-09-23", periods=2))
    monkeypatch.setattr(kb, "load_kospi_history", lambda start, end=None: (fake, "FDR:YAHOO:^KS11"))
    ab = _load_script("ab_exit_policy.py", "_ab_exit_for_bench_test")
    out = ab.benchmark_return("2026-09-22", "2026-09-23")
    assert out["code"] == "FDR:YAHOO:^KS11" and round(out["return_pct"], 6) == 10.0


@pytest.mark.parametrize("last_bar,at,ok", [
    (None, 1, False), (None, 59, False),                  # 커서 없음 → 이상치와 무관하게 대용 채택 안 함
    ("2026-09-17", 35, False), ("2026-09-17", 34, True),  # 커서 뒤 4봉(09-18·21·22·23) + 21 = 25
])
def test_harvest_rejects_proxy_outlier_inside_judgment_window(monkeypatch, harvest_env, last_bar, at, ok):
    tmp_path, logs = harvest_env
    cursor = tmp_path / "cursor.json"
    before = None
    if last_bar is not None:
        before = f'{{"last_bar": "{last_bar}", "last_d0": {{}}}}'
        cursor.write_text(before, encoding="utf-8")
    fetch, _ = stub(**{"YAHOO:^KS11": nan_last("2026-09-23"), "KS11": frame("2026-09-17"),
                       "069500": frame("2026-09-23", shifted(KODEX, at))})
    monkeypatch.setattr(kb, "_fdr_fetch", fetch)
    monkeypatch.setattr(hs, "_load_universe", lambda bt: [])
    monkeypatch.setattr(hs, "_process", lambda bt, data, ok_dates, universe, pending, positions, cur:
                        ([], 0, cur))

    success, message = asyncio.run(hs.run_daily_shadow_scan())
    assert success is ok
    if not ok:
        at_date = pd.bdate_range(end="2026-09-23", periods=60)[at].date()
        assert ("reason=proxy_needs_cursor" in message if last_bar is None else
                "reason=proxy_return_outlier" in message and f"이상치 {at_date} +" in message)
        assert (cursor.read_text(encoding="utf-8") if cursor.exists() else None) == before
        assert not (tmp_path / "pending.json").exists()
        assert not any("체제 게이트 원천" in msg for _, msg in logs)
