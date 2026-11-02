"""E02-CORE-INDICATORS — 코어 리밸런싱 BUY 신호의 선정 당시 지표 전달 (2026-11-02).

보고된 공백: 11/2 09:07:44 011200 코어 리밸런싱 BUY 는 `CoreCandidate.indicators` 에 PER/PBR/수급/MA 가 있었지만
`Signal.metadata` 에 실리지 않아 크로스검증이 `지표결손(ATR,PER,PBR,수급) -8` 을 적용했다.
여기서는 실제 생산자(`BatchAnalyzer.execute_core_rebalance`) → `SignalEvent` → 실제 소비자(`CrossValidator.validate`)
경로로 전달 공백이 닫혔는지, 그리고 결손·불리한 지표·기존 metadata·객체 수명·구 pending 항목이 그대로인지 확인한다.
전부 합성 입력 — 네트워크·운영 캐시 무접촉(Path.home 은 tmp_path).
"""
from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.analytics.swing_supply import KST, daily_evidence, previous_session, utc_now  # noqa: E402
from src.core.batch_analyzer import BatchAnalyzer, core_signal_indicators  # noqa: E402
from src.core.cross_validator import CrossStrategyValidator as CrossValidator  # noqa: E402
from src.core.entry_llm_context import build_entry_context  # noqa: E402
from src.core.types import OrderSide, Position  # noqa: E402
from src.signals.screener.core_screener import CoreCandidate, CoreScreener  # noqa: E402
from src.strategies.exit_manager import ExitConfig, ExitManager  # noqa: E402
from test_risk_sizing import home  # noqa: E402,F401

SYMBOL = "011200"
HELD = "033780"


def _ind(**over):
    """core_screener._calc_indicators + _enrich_supply_demand 가 만드는 형태 그대로(실측 11/2 키 구성)."""
    base = {
        "close": 23600.0, "ma5": 23100.0, "ma20": 22400.0, "ma50": 21200.0, "ma200": 19050.0,
        "ma_aligned": True, "ma5_above_ma20": True, "ma200_below_days": 0, "ret_vol_60d": 0.0214,
        "avg_trading_value": 4.2e10, "per": 16.2, "pbr": 0.9, "eps": 1450.0, "bps": 26000.0,
        "rank": 40, "market_cap_eok": 120000, "sector": "운송",
        "foreign_net_buy_5d": 312000, "inst_net_buy_5d": -45000,
        "foreign_net_buy_5d_observed_days": 5, "inst_net_buy_5d_observed_days": 5,
    }
    base.update(over)
    return base


def _candidate(ind, symbol=SYMBOL, score=85.0):
    return CoreCandidate(symbol=symbol, name="HMM", score=score, entry_price=Decimal("23600"),
                         indicators=ind, reasons=["MA정배열", "MA200↑", "6M+12.6%"])


def _ba(tmp_path, candidates, positions=None):
    emitted = []

    async def _emit(event):
        emitted.append(event)

    async def _no_daily(*_a, **_k):
        return None

    async def _scan():
        return list(candidates)

    ba = object.__new__(BatchAnalyzer)
    ba._config = {"core_holding": {"enabled": True, "max_positions": 3}}
    ba._engine = SimpleNamespace(portfolio=SimpleNamespace(positions=positions if positions is not None else {}),
                                 emit=_emit, risk_manager=None)
    ba._broker = SimpleNamespace(get_daily_prices=_no_daily)
    ba._exit_manager = ExitManager(ExitConfig())
    ba._core_screener = SimpleNamespace(run_full_scan=_scan)
    ba._core_strategy = object()
    ba._core_state_path = tmp_path / "core_state.json"
    return ba, emitted


def _validate(metadata, score=85.0):
    cv = CrossValidator(market="KR")
    return cv.validate(SYMBOL, "buy", "core_holding", score, metadata, market_regime="neutral",
                       count_stats=False)


def _legacy(metadata):
    """수정 전 생산자가 만들던 metadata(지표 없음)."""
    return {k: v for k, v in metadata.items() if k != "indicators"}


# CrossStrategyValidator.validate 는 통과 신호에 reason="" 을 돌려주고 감점 내역은 로그에만 남긴다.
# 따라서 전달 효과는 같은 시각에 평가한 수정 전(metadata 지표 없음) 대비 조정 점수 차이로 확인한다
# (시간대 가점 등 양쪽에 똑같이 붙는 규칙은 차이에서 상쇄된다).
MISSING_ALL = 8      # ATR·PER·PBR·수급 4개 결손 × 2 (상한 8)
MISSING_ATR = 2      # 코어 스크리너는 ATR 을 계산하지 않는다


# ── 헬퍼 계약 ─────────────────────────────────────────────────────────────

def test_helper_forwards_consumer_keys_and_preserves_missing():
    ind = _ind(ma20=None)                      # 캔들 부족으로 None 인 값은 None 그대로
    out = core_signal_indicators(ind)
    assert out["per"] == 16.2 and out["pbr"] == 0.9 and out["close"] == 23600.0 and out["ma200"] == 19050.0
    assert out["ma20"] is None
    # 소비자 키 별칭 + 코어 원키 + 창 일수
    assert out["foreign_net_buy"] == 312000 and out["inst_net_buy"] == -45000
    assert out["foreign_net_buy_5d"] == 312000 and out["supply_window_days"] == 5
    assert out["indicators_source"] == "core_screener"
    # 코어 스크리너가 계산하지 않는 ATR 은 만들지 않는다
    assert not any(k.startswith("atr") for k in out)
    # 문자열 섹터·순위 센티널은 소비자 키가 아니므로 싣지 않는다
    assert "sector" not in out and "rank" not in out
    # 후보 dict 와 공유하지 않는다
    ind["per"] = 99.0
    ind["foreign_net_buy_5d"] = -1
    assert out["per"] == 16.2 and out["foreign_net_buy"] == 312000


def test_helper_zero_sentinel_and_empty_input():
    # 유니버스 미제공 센티널 0.0 → 결측(None); 음수 PER(적자)는 값 보존
    out = core_signal_indicators(_ind(per=0.0, pbr=0))
    assert out["per"] is None and out["pbr"] is None
    assert core_signal_indicators(_ind(per=-7.5))["per"] == -7.5
    # 수급 키가 아예 없으면 별칭·창 일수도 없다(가짜 결측/존재 표시 없음)
    ind = _ind()
    del ind["foreign_net_buy_5d"]
    del ind["inst_net_buy_5d"]
    out = core_signal_indicators(ind)
    assert "foreign_net_buy" not in out and "supply_window_days" not in out
    # 수급 키가 있고 값이 None 이면 None 보존(크로스검증은 결손으로 읽는다)
    out = core_signal_indicators(_ind(foreign_net_buy_5d=None, inst_net_buy_5d=None,
                                      foreign_net_buy_5d_observed_days=None, inst_net_buy_5d_observed_days=None))
    assert out["foreign_net_buy"] is None and out["inst_net_buy"] is None
    # 원천이 미관측 일자를 0 으로 채운 합계(관측 일수 0)는 소비자 키에 "0" 으로 싣지 않는다 — 원키는 보존
    out = core_signal_indicators(_ind(foreign_net_buy_5d=0, foreign_net_buy_5d_observed_days=0,
                                      inst_net_buy_5d=-45000, inst_net_buy_5d_observed_days=3))
    assert out["foreign_net_buy"] is None and out["foreign_net_buy_5d"] == 0 and out["foreign_net_buy_5d_observed_days"] == 0
    assert out["inst_net_buy"] == -45000 and out["inst_net_buy_5d_observed_days"] == 3
    # 관측 일수 키가 없거나 bool/비정수면 미확인 → None
    legacy = _ind()
    del legacy["foreign_net_buy_5d_observed_days"]
    assert core_signal_indicators(legacy)["foreign_net_buy"] is None
    assert core_signal_indicators(_ind(foreign_net_buy_5d_observed_days=True))["foreign_net_buy"] is None
    # 빈 입력 → 빈 dict (출처 표시도 없음)
    assert core_signal_indicators({}) == {} and core_signal_indicators(None) == {}
    # Decimal 은 JSON 가능한 float 로, 직렬화 불가 객체는 None
    out = core_signal_indicators(_ind(close=Decimal("23600"), ma5=object()))
    assert out["close"] == 23600.0 and isinstance(out["close"], float) and out["ma5"] is None
    json.dumps(out)


# ── 생산자 → 소비자 E2E ─────────────────────────────────────────────────────

def test_rebalance_buy_signal_closes_missing_indicator_gap(home, tmp_path):
    cand = _candidate(_ind())
    ba, emitted = _ba(tmp_path, [cand])

    assert asyncio.run(ba.execute_core_rebalance()) is True
    assert [(e.symbol, e.side) for e in emitted] == [(SYMBOL, OrderSide.BUY)]
    meta = emitted[0].metadata
    # 기존 metadata·가격·손절은 그대로
    assert meta["is_core"] is True and meta["rebalance"] is True and meta["batch_signal"] is True
    assert meta["candidate_name"] == "HMM"
    assert emitted[0].price == Decimal("23600")
    assert emitted[0].stop_price == Decimal("23600") * Decimal("0.85")
    assert "quantity" not in meta                      # 수량·현금 판단은 엔진 on_signal 몫 그대로

    ok_new, adj_new, _ = _validate(meta)
    ok_old, adj_old, _ = _validate(_legacy(meta))
    assert ok_new and ok_old
    # 수정 전: ATR·PER·PBR·수급 4개 결손 -8 / 수정 후: 코어가 계산하지 않는 ATR 만 결손 -2
    assert adj_old <= 85.0 - MISSING_ALL
    assert adj_new - adj_old == MISSING_ALL - MISSING_ATR
    # 전달된 값은 선정 당시 값과 같은 의미로 소비된다(PER 16.2·PBR 0.9 는 규칙 8 비대상 → 추가 감점 없음)
    assert adj_new == adj_old + MISSING_ALL - MISSING_ATR

    # 객체 수명: 발행 뒤 후보 지표가 바뀌어도 이미 발행된 신호는 불변
    cand.indicators["per"] = 80.0
    cand.indicators["foreign_net_buy_5d"] = -999
    assert meta["indicators"]["per"] == 16.2 and meta["indicators"]["foreign_net_buy"] == 312000


def test_unfavorable_indicators_are_not_neutralized_by_forwarding(home, tmp_path):
    cand = _candidate(_ind(per=-5.0, pbr=6.2, foreign_net_buy_5d=-120000, inst_net_buy_5d=-30000))
    ba, emitted = _ba(tmp_path, [cand])
    asyncio.run(ba.execute_core_rebalance())
    meta = emitted[0].metadata

    ok_new, adj_new, _ = _validate(meta)
    ok_old, adj_old, _ = _validate(_legacy(meta))
    # 적자+고PBR 규칙 8(-10)이 그대로 적용되어 수정 전(결손 -8)보다 낮아진다 — 전달은 "+8" 이 목적이 아니다
    assert adj_new == adj_old + (MISSING_ALL - MISSING_ATR) - 10
    assert adj_new < adj_old
    # 코어 전략은 규칙 2(동시 순매도 차단/감점) 대상이 아니므로 차단되지 않는다(정책 불변)
    assert ok_new is True


def test_partial_or_missing_indicators_keep_missing_penalty(home, tmp_path):
    partial = _candidate(_ind(per=0.0, foreign_net_buy_5d=None, inst_net_buy_5d=None))
    empty = _candidate({}, symbol="033780", score=84.0)
    ba, emitted = _ba(tmp_path, [partial, empty])
    asyncio.run(ba.execute_core_rebalance())
    by_symbol = {e.symbol: e.metadata for e in emitted}

    # PER 센티널 0·수급 None → 결손으로 유지(PBR 만 전달됨 → ATR·PER·수급 3개 결손 -6)
    ind = by_symbol[SYMBOL]["indicators"]
    assert ind["per"] is None and ind["foreign_net_buy"] is None and ind["pbr"] == 0.9
    _, adj_partial, _ = _validate(by_symbol[SYMBOL])
    _, adj_partial_legacy, _ = _validate(_legacy(by_symbol[SYMBOL]))
    assert adj_partial == adj_partial_legacy + MISSING_ALL - 6
    # 지표가 아예 없는 후보는 수정 전과 같은 -8, metadata 도 빈 dict
    assert by_symbol["033780"]["indicators"] == {}
    _, adj_empty, _ = _validate(by_symbol["033780"], score=84.0)
    _, adj_empty_legacy, _ = _validate(_legacy(by_symbol["033780"]), score=84.0)
    assert adj_empty == adj_empty_legacy


# ── 2단계 리밸런싱(pending) 저장·재시도 ──────────────────────────────────────

def test_pending_buys_persist_indicators_and_retry_restores_them(home, tmp_path):
    # 보유 코어가 재스코어 40(<55) → 교체 매도 → 매수는 pending 저장
    held = Position(symbol=HELD, quantity=10, avg_price=Decimal("100000"),
                    current_price=Decimal("100000"), strategy="core_holding")
    rescore = _candidate(_ind(), symbol=HELD, score=40.0)
    buy = _candidate(_ind())
    ba, emitted = _ba(tmp_path, [rescore, buy], positions={HELD: held})

    assert asyncio.run(ba.execute_core_rebalance()) is False     # 매수 미완료(매도 체결 대기)
    assert [(e.symbol, e.side) for e in emitted] == [(HELD, OrderSide.SELL)]
    state = json.loads((tmp_path / "core_state.json").read_text())
    pend = state["pending_core_buys"]
    assert pend[0]["symbol"] == SYMBOL and pend[0]["indicators"] == core_signal_indicators(_ind())

    # 다음 윈도우: 매도 체결(보유 해제) 뒤 재시도 — 저장된 지표가 그대로 실리고, 구 항목(지표 없음)은 만들지 않는다
    legacy_entry = {"symbol": "012750", "name": "에스원", "score": 72.0, "entry_price": "150000",
                    "reasons": ["MA정배열"]}
    state["pending_core_buys"] = [pend[0], legacy_entry]
    state["sold"] = []
    (tmp_path / "core_state.json").write_text(json.dumps(state))
    ba2, emitted2 = _ba(tmp_path, [])
    assert asyncio.run(ba2.execute_core_rebalance()) is True
    by_symbol = {e.symbol: e for e in emitted2}
    assert by_symbol[SYMBOL].metadata["indicators"] == pend[0]["indicators"]
    assert by_symbol[SYMBOL].metadata["rebalance"] is True and by_symbol[SYMBOL].price == Decimal("23600")
    assert "indicators" not in by_symbol["012750"].metadata
    _, adj_retry, _ = _validate(by_symbol[SYMBOL].metadata)
    _, adj_retry_legacy, _ = _validate(_legacy(by_symbol[SYMBOL].metadata))
    assert adj_retry == adj_retry_legacy + MISSING_ALL - MISSING_ATR


# ── 생산자(코어 스크리너) 수급 보강: 관측 일수 ───────────────────────────────

def _daily_rows(now, spec):
    """spec: [(foreign_raw|None, inst_raw|None), ...] 최근 거래일부터. None 은 원천 미관측(수량 0 채움·evidence None)."""
    captured = now.isoformat()
    day = now.astimezone(KST).date()
    rows = {}
    for foreign_raw, inst_raw in spec:
        day = previous_session(day)
        key = day.strftime("%Y%m%d")
        foreign = daily_evidence(foreign_raw, key, captured) if foreign_raw is not None else None
        inst = daily_evidence(inst_raw, key, captured) if inst_raw is not None else None
        rows[key] = {"foreign_net_buy": foreign["value"] if foreign is not None else 0,
                     "inst_net_buy": inst["value"] if inst is not None else 0,
                     "_supply_evidence": {"foreign": foreign, "institution": inst}}
    return rows


def test_core_screener_supply_enrichment_counts_observed_days(home):
    now = utc_now()
    observed = _daily_rows(now, [(1000, -200), (2000, None), (None, None), (0, 50), (-500, 10)])
    unobserved = _daily_rows(now, [(None, None)] * 5)

    async def _fetch(symbol, days=5):
        return {"011200": observed, "033780": unobserved}[symbol]

    screener = object.__new__(CoreScreener)
    screener._kis_market_data = SimpleNamespace(fetch_stock_investor_daily=_fetch)
    a = CoreCandidate(symbol="011200", name="HMM", indicators={})
    b = CoreCandidate(symbol="033780", name="KT&G", indicators={})
    asyncio.run(screener._enrich_supply_demand([a, b]))

    # 합계는 기존 그대로(원천 0 채움 포함) — 점수 입력 불변
    assert a.indicators["foreign_net_buy_5d"] == 2500 and a.indicators["inst_net_buy_5d"] == -140
    # 관측 일수는 유효 evidence 가 있는 날만 센다(값 0 인 관측일도 관측일)
    assert a.indicators["foreign_net_buy_5d_observed_days"] == 4
    assert a.indicators["inst_net_buy_5d_observed_days"] == 3
    assert b.indicators["foreign_net_buy_5d"] == 0 and b.indicators["foreign_net_buy_5d_observed_days"] == 0
    # 신호 전달: 관측된 종목은 소비자 키에 실리고, 전부 미관측인 종목은 None(결손 유지)
    assert core_signal_indicators(a.indicators)["foreign_net_buy"] == 2500
    out_b = core_signal_indicators(b.indicators)
    assert out_b["foreign_net_buy"] is None and out_b["inst_net_buy"] is None and out_b["foreign_net_buy_5d"] == 0


def test_unobserved_supply_sum_stays_missing_for_cross_validation(home, tmp_path):
    cand = _candidate(_ind(foreign_net_buy_5d=0, inst_net_buy_5d=0,
                           foreign_net_buy_5d_observed_days=0, inst_net_buy_5d_observed_days=0))
    ba, emitted = _ba(tmp_path, [cand])
    asyncio.run(ba.execute_core_rebalance())
    meta = emitted[0].metadata
    _, adj_new, _ = _validate(meta)
    _, adj_old, _ = _validate(_legacy(meta))
    # PER·PBR 만 채워지고 ATR·수급은 결손 유지 → -8 → -4
    assert adj_new == adj_old + MISSING_ALL - 4


# ── entry LLM 공통 입력: 집계 창·출처 보존 ──────────────────────────────────

def _facts(context):
    return json.loads(context.split("<entry_facts>\n", 1)[1].split("\n</entry_facts>", 1)[0])


def test_entry_llm_input_keeps_supply_window_and_source(home, tmp_path):
    ba, emitted = _ba(tmp_path, [_candidate(_ind())])
    asyncio.run(ba.execute_core_rebalance())
    ind = emitted[0].metadata["indicators"]
    flow = _facts(build_entry_context(SYMBOL, "core_holding", 85, ind, "neutral", "운송"))["indicators"]["foreign_net_buy"]
    assert flow["value"] == "312000" and flow["direction"] == "net_buy"
    assert flow["window_days"] == 5 and flow["source"] == "core_screener"
    # 스윙 경로(당일 값, 창/출처 미기재)는 None=미확인으로 남는다
    flow_swing = _facts(build_entry_context("005930", "sepa_trend", 80, {"foreign_net_buy": 120}, "bull", ""))["indicators"]["foreign_net_buy"]
    assert flow_swing["window_days"] is None and flow_swing["source"] is None
    # 미관측 코어 수급은 direction=unknown 으로 전달된다(balanced 아님)
    flow_none = _facts(build_entry_context(SYMBOL, "core_holding", 85,
                                           core_signal_indicators(_ind(foreign_net_buy_5d=0, foreign_net_buy_5d_observed_days=0)),
                                           "neutral", "운송"))["indicators"]["foreign_net_buy"]
    assert flow_none["direction"] == "unknown" and flow_none["status"] == "missing" and flow_none["window_days"] == 5
