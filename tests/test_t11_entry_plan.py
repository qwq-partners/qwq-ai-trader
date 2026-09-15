"""T11 담당 C — 조건부 진입계획(EntryPlan) shadow 실행 검증 (2026-09-15)

계약: `docs/superpowers/plans/2026-09-15-agent-team-evidence-entryplan.md` §2.3,
인수 조건 §4 #3·#7·#8·#9·#10.

여기서 검증하는 것:
    check_entry_plan 순수 판정 → PendingSignal 생성부(계획 채움) → JSON 왕복 →
    Signal.metadata["entry_plan"] → SignalEvent 직렬화 → engine.on_signal shadow 훅(기록만)
    → kr_scheduler 팀 후보/보유 dict 계약

돈 경로 불변(플래그 off/on 주문 동일)은 tests/test_t11_money_path_baseline.py 소관.
프로덕션 캐시·네트워크 무접촉 — Path.home() 은 tmp_path, 시각·호가는 전부 주입.
실행: venv/bin/python -m pytest tests/test_t11_entry_plan.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from src.core.batch_analyzer import PendingSignal  # noqa: E402
from src.execution.entry_plan import (  # noqa: E402
    QUOTE_MAX_AGE_SEC, PlanCheck, check_entry_plan,
)
from test_risk_sizing import home  # noqa: E402,F401  (tmp_path 를 Path.home() 으로 주입)

NOW = datetime(2026, 9, 15, 10, 30, 0)
SYM = "005930"


def _freeze_clock(monkeypatch, module, fixed: datetime = NOW) -> None:
    """모듈의 `datetime.now()` 를 NOW 로 동결 — 배치 실행부의 14:30 SEPA 진입 차단·계획 만료
    (NOW+5h=15:30)·엔진 shadow 검증의 만료 판정이 실제 벽시계에 따라 갈라지지 않게 한다
    (2026-09-15 15:33 배포 verify 가 장 마감 후 실행돼 7건이 실패·자동 롤백된 원인)."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.replace(tzinfo=tz)
    monkeypatch.setattr(module, "datetime", _Frozen)


def _plan(**kw) -> PendingSignal:
    """합성 계획 — 기본값은 '전부 통과'(allow) 이고 테스트가 한 조건씩 깨뜨린다."""
    base = dict(
        symbol=SYM, name="삼성전자", strategy="sepa_trend", side="buy",
        entry_price=10000.0, max_entry_price=10300.0, stop_price=9500.0,
        target_price=11000.0, score=70.0, reason="추세",
        created_at=(NOW - timedelta(hours=2)).isoformat(),
        expires_at=(NOW + timedelta(hours=5)).isoformat(),
        plan_id="abc123def456", setup="sepa_pullback",
        decided_at=(NOW - timedelta(hours=2)).isoformat(),
        invalidation={"stop_price": 9500.0, "below_price": 0.0,
                      "intraday_levels": ["severe"],
                      "expires_at": (NOW + timedelta(hours=5)).isoformat()},
        trigger={"type": "none"},
        assumptions={"fee_bps": 1.40527, "slippage_bps": None, "liquidity_ok": None},
        exit_policy_ref="exit_manager:sepa_trend",
    )
    base.update(kw)
    return PendingSignal(**base)


def _quote(price=10100.0, age_sec=10, **kw):
    q = {"price": price, "as_of": NOW - timedelta(seconds=age_sec)}
    q.update(kw)
    return q


# ── 1) 순수 판정 (§4 #8) ──────────────────────────────────────────────────────

def test_all_conditions_met_allows():
    chk = check_entry_plan(_plan(), _quote(), NOW)
    assert isinstance(chk, PlanCheck)
    assert chk.status == "allow"
    assert chk.reasons == []
    assert chk.expected_fill_price == 10100.0
    assert chk.plan_id == "abc123def456" and chk.setup == "sepa_pullback"
    assert chk.checked_at == NOW and chk.quote_as_of is not None


def test_dict_plan_is_accepted_same_as_dataclass():
    """팀 심의는 후보 dict 의 entry_plan(직렬화된 계획)으로 같은 함수를 호출한다."""
    assert check_entry_plan(_plan().to_dict(), _quote(), NOW).status == "allow"


def test_expired_plan_rejects():
    chk = check_entry_plan(_plan(expires_at=(NOW - timedelta(minutes=1)).isoformat()),
                           _quote(), NOW)
    assert chk.status == "reject" and "PLAN_EXPIRED" in chk.reasons


def test_invalidation_expires_at_also_rejects():
    p = _plan()
    p.invalidation = dict(p.invalidation, expires_at=(NOW - timedelta(minutes=1)).isoformat())
    chk = check_entry_plan(p, _quote(), NOW)
    assert chk.status == "reject" and "PLAN_EXPIRED" in chk.reasons


def test_quote_missing_waits():
    assert check_entry_plan(_plan(), None, NOW).status == "wait"
    assert "QUOTE_MISSING" in check_entry_plan(_plan(), None, NOW).reasons
    assert "QUOTE_MISSING" in check_entry_plan(_plan(), {"price": 0}, NOW).reasons


def test_quote_without_as_of_is_stale():
    chk = check_entry_plan(_plan(), {"price": 10100.0}, NOW)
    assert chk.status == "wait" and "QUOTE_STALE" in chk.reasons
    assert chk.quote_as_of is None


def test_quote_older_than_max_age_is_stale():
    ok = check_entry_plan(_plan(), _quote(age_sec=QUOTE_MAX_AGE_SEC), NOW)
    old = check_entry_plan(_plan(), _quote(age_sec=QUOTE_MAX_AGE_SEC + 1), NOW)
    assert ok.status == "allow"
    assert old.status == "wait" and "QUOTE_STALE" in old.reasons


def test_future_quote_is_not_trusted():
    chk = check_entry_plan(_plan(), _quote(age_sec=-60), NOW)
    assert chk.status == "wait" and "QUOTE_STALE" in chk.reasons


def test_price_above_cap_waits_not_rejects():
    """상한 초과는 wait — 장중 눌리면 밴드로 복귀할 수 있고, 폐기는 CARRY_REASONS 소관."""
    chk = check_entry_plan(_plan(), _quote(price=10301.0), NOW)
    assert chk.status == "wait" and "PRICE_ABOVE_CAP" in chk.reasons


def test_price_below_band_waits():
    chk = check_entry_plan(_plan(entry_band_low=10000.0), _quote(price=9999.0), NOW)
    assert chk.status == "wait" and "PRICE_BELOW_BAND" in chk.reasons
    # 하한 0 = 하한 없음 (0 을 falsy 로 흘리면 모든 계획이 밴드 미달이 된다)
    assert check_entry_plan(_plan(entry_band_low=0.0), _quote(price=9600.0), NOW).reasons == []


def test_trigger_not_met_waits_and_strict_greater_than():
    p = _plan(setup="vcp_breakout", strategy="vcp_breakout", entry_mode="breakout",
              breakout_trigger=10200.0,
              trigger={"type": "breakout", "level": 10200.0, "satisfied": None})
    below = check_entry_plan(p, _quote(price=10199.0), NOW)
    equal = check_entry_plan(p, _quote(price=10200.0), NOW)
    above = check_entry_plan(p, _quote(price=10201.0), NOW)
    assert below.status == "wait" and "TRIGGER_NOT_MET" in below.reasons
    assert equal.status == "wait", "트리거 '초과' 조건 — 동일가는 미달"
    assert above.status == "allow"


def test_vcp_without_trigger_level_waits_input_missing():
    p = _plan(setup="vcp_breakout", strategy="vcp_breakout", trigger={"type": "breakout"})
    chk = check_entry_plan(p, _quote(), NOW)
    assert chk.status == "wait" and "INPUT_MISSING:trigger.level" in chk.reasons
    assert "trigger.level" in chk.missing_inputs


def test_gap_vwap_requires_vwap():
    p = _plan(setup="gap_vwap", strategy="gap_and_go")
    no_vwap = check_entry_plan(p, _quote(), NOW)
    assert no_vwap.status == "wait" and "INPUT_MISSING:vwap" in no_vwap.reasons
    assert no_vwap.missing_inputs == ["vwap"]
    assert check_entry_plan(p, _quote(vwap=10050.0), NOW).status == "allow"


def test_declared_missing_inputs_are_reported():
    chk = check_entry_plan(_plan(missing_inputs=["rs_rating"]), _quote(), NOW)
    assert chk.status == "wait" and "INPUT_MISSING:rs_rating" in chk.reasons


def test_intraday_severe_rejects_crash_waits_only_for_sepa():
    sepa = _plan()
    # 비 SEPA 대조군은 트리거 필수 setup 을 피한다(그러면 INPUT_MISSING 으로 wait 가 된다)
    other = _plan(strategy="momentum_breakout", setup="momentum")
    assert check_entry_plan(sepa, _quote(), NOW, intraday_level="severe").status == "reject"
    assert "INTRADAY_BLOCK:severe" in check_entry_plan(
        sepa, _quote(), NOW, intraday_level="severe").reasons
    # crash 는 기존 batch_analyzer 규칙대로 SEPA 계열만 — 새 차단을 추가하지 않는다
    assert check_entry_plan(sepa, _quote(), NOW, intraday_level="crash").status == "wait"
    assert check_entry_plan(other, _quote(), NOW, intraday_level="crash").status == "allow"
    assert check_entry_plan(sepa, _quote(), NOW, intraday_level="caution").status == "allow"


def test_invalidated_below_stop_rejects():
    chk = check_entry_plan(_plan(), _quote(price=9500.0), NOW)
    assert chk.status == "reject" and "INVALIDATED:stop_price" in chk.reasons


def test_risk_ctx_only_judged_when_given():
    assert check_entry_plan(_plan(), _quote(), NOW, risk_ctx=None).status == "allow"
    assert check_entry_plan(_plan(), _quote(), NOW, risk_ctx={}).status == "allow"
    for ctx, code in (({"risk_budget_ok": False}, "RISK_BUDGET"),
                      ({"slots_available": 0}, "SLOT_FULL"),
                      ({"daily_buys_left": 0}, "DAILY_LIMIT")):
        chk = check_entry_plan(_plan(), _quote(), NOW, risk_ctx=ctx)
        assert chk.status == "wait" and code in chk.reasons


def test_expected_fill_uses_ask_when_available():
    chk = check_entry_plan(_plan(), _quote(price=10100.0, ask=10120.0), NOW)
    assert chk.expected_fill_price == 10120.0


def test_cost_adjusted_rr_records_value_without_changing_status():
    """COST_RR_LOW 는 사유만 부착 — 컷오프 정책값은 미정(보류)."""
    chk = check_entry_plan(_plan(), _quote(price=10100.0), NOW)
    cost = 10100.0 * 1.40527 / 10000.0
    expected = (11000.0 - 10100.0 - cost) / (10100.0 - 9500.0 + cost)
    assert chk.cost_adjusted_rr == pytest.approx(expected)
    assert chk.status == "allow" and "COST_RR_LOW" not in chk.reasons

    low = check_entry_plan(_plan(target_price=10200.0), _quote(price=10100.0), NOW)
    assert low.cost_adjusted_rr < 1.0
    assert "COST_RR_LOW" in low.reasons
    assert low.status == "allow", "손익비는 status 를 바꾸지 않는다"


def test_reject_wins_over_wait():
    chk = check_entry_plan(_plan(expires_at=(NOW - timedelta(minutes=1)).isoformat()),
                           _quote(price=99999.0), NOW)
    assert chk.status == "reject"


def test_checker_never_raises():
    for bad in (None, 3, "x", {"expires_at": "not-a-date", "max_entry_price": "x"}):
        chk = check_entry_plan(bad, _quote(), NOW)
        assert chk.status in ("allow", "wait", "reject")
    # 지뢰 객체(모든 속성 접근이 예외) 도 CHECKER_ERROR 로 격리
    class _Boom:
        def to_dict(self):
            raise RuntimeError("boom")
    chk = check_entry_plan(_Boom(), _quote(), NOW)
    assert chk.status == "wait" and chk.reasons[0].startswith("CHECKER_ERROR:")


def test_plan_check_to_dict_is_json_serializable():
    d = check_entry_plan(_plan(), _quote(), NOW).to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["checked_at"] == NOW.isoformat(timespec="seconds")
    assert set(d) >= {"status", "reasons", "expected_fill_price", "cost_adjusted_rr",
                      "checked_at", "quote_as_of", "missing_inputs", "plan_id", "setup"}


# ── 2) 계획 생성·직렬화 왕복 (§4 #7) ─────────────────────────────────────────

def test_plan_survives_json_roundtrip():
    p = _plan(entry_band_low=9900.0, required_inputs=["vwap"], inputs_ref={"score": 70.0})
    back = PendingSignal.from_dict(json.loads(json.dumps(p.to_dict())))
    assert back.to_dict() == p.to_dict()
    assert check_entry_plan(back, _quote(), NOW).status == check_entry_plan(p, _quote(), NOW).status


def test_legacy_json_without_plan_fields_still_loads():
    """계획이 없는 구 JSON 은 그대로 로드되고, 검증기가 계획을 지어내지 않는다."""
    legacy = {"symbol": SYM, "name": "삼", "strategy": "sepa_trend", "side": "buy",
              "entry_price": 10000.0, "max_entry_price": 10300.0, "stop_price": 9500.0,
              "target_price": 11000.0, "score": 70.0, "reason": "r",
              "created_at": NOW.isoformat(), "expires_at": (NOW + timedelta(hours=5)).isoformat()}
    back = PendingSignal.from_dict(legacy)
    assert back.plan_id == "" and back.setup == "" and back.trigger == {}


@pytest.mark.parametrize("strategy,entry_mode,expected", [
    ("sepa_trend", "close", "sepa_pullback"),
    ("vcp_breakout", "breakout", "vcp_breakout"),
    # 전략 매핑이 먼저다 — breakout 모드가 전략 고유 setup 을 덮어쓰면
    # gap_and_go 에 돌파 모드가 생겼을 때 vwap 필수 조건이 조용히 사라진다
    ("sepa_trend", "breakout", "sepa_pullback"),
    ("무명전략", "breakout", "vcp_breakout"),
    ("gap_and_go", "close", "gap_vwap"),
    ("rsi2_reversal", "close", "rsi2_reversal"),
    ("momentum_breakout", "close", "momentum"),
    ("core_holding", "close", ""),
])
def test_setup_mapping(strategy, entry_mode, expected):
    from src.core.batch_analyzer import setup_for_strategy
    assert setup_for_strategy(strategy, entry_mode) == expected


def _analyzer(monkeypatch, tmp_path):
    """BatchAnalyzer 최소 구성 — __init__(스크리너·전략·프로바이더) 우회."""
    from src.core.batch_analyzer import BatchAnalyzer
    ba = object.__new__(BatchAnalyzer)
    ba._config = {}
    ba._market_regime = "neutral"
    ba._intraday_state = "normal"
    ba._intraday_kospi_pct = 0.0
    ba._intraday_recovery_until = None
    ba._max_entry_slippage_pct = 3.0
    ba._slippage_by_regime = {"bull": 5.0, "neutral": 3.0, "bear": 3.0}
    ba._unknown_regime_warned = set()
    ba._pending = []
    ba._save_json = lambda: None
    ba._load_json = lambda: list(ba._pending)
    return ba


def test_generated_plan_fields(monkeypatch, tmp_path):
    """생성부가 plan_id/setup/decided_at/trigger/invalidation/assumptions 를 채운다."""
    from decimal import Decimal
    from src.core.batch_analyzer import BatchAnalyzer
    from src.core.types import OrderSide, Signal, SignalStrength, StrategyType

    ba = _analyzer(monkeypatch, tmp_path)
    sig = Signal(symbol=SYM, side=OrderSide.BUY, strength=SignalStrength.STRONG,
                 strategy=StrategyType.VCP_BREAKOUT, price=Decimal("10000"),
                 stop_price=Decimal("9600"), target_price=Decimal("11000"), score=72.0,
                 reason="VCP 돌파",
                 metadata={"candidate_name": "삼성전자", "atr_pct": 2.5,
                           "entry_mode": "breakout", "breakout_trigger": 10200.0,
                           "indicators": {"rs_rating": 88}})
    p = BatchAnalyzer._to_pending_signal(ba, sig, now=NOW,
                                         expires=NOW + timedelta(hours=5), slippage_pct=3.0)
    assert len(p.plan_id) == 12 and p.plan_version == 1
    assert p.setup == "vcp_breakout"
    assert p.decided_at == NOW.isoformat()
    assert p.candidate_id and SYM in p.candidate_id
    assert p.trigger == {"type": "breakout", "level": 10200.0,
                         "satisfied": None, "satisfied_at": None}
    assert p.invalidation["stop_price"] == 9600.0
    assert p.invalidation["expires_at"] == p.expires_at
    assert p.invalidation["intraday_levels"] == ["severe"]
    assert p.exit_policy_ref == "exit_manager:vcp_breakout"
    assert p.assumptions["fee_bps"] == pytest.approx(1.40527)
    # 모르는 값은 지어내지 않는다
    assert p.assumptions["slippage_bps"] is None and p.assumptions["liquidity_ok"] is None
    assert p.inputs_ref["score"] == 72.0
    assert set(p.inputs_ref["indicator_keys"]) == {"rs_rating"}
    # 출처가 없으면 None — now 로 채우지 않는다
    assert p.inputs_ref["indicators_as_of"] is None
    assert p.assumptions["fee_side"] == "buy_only"   # 매수측 비용만 반영했음을 명시
    # 계획 id 는 후보마다 다르다
    p2 = BatchAnalyzer._to_pending_signal(ba, sig, now=NOW,
                                          expires=NOW + timedelta(hours=5), slippage_pct=3.0)
    assert p2.plan_id != p.plan_id


def test_plan_indicators_as_of_from_indicator_cache(monkeypatch, tmp_path):
    """지표 dict 에 'as_of' 가 없으면 실제 출처인 TechnicalIndicators._cache_ts 를 읽는다.

    운영 생산자(calculate_all)는 'as_of' 키를 만들지 않는다 — 1차 리뷰 blocking 2.
    """
    from decimal import Decimal
    from src.core.batch_analyzer import BatchAnalyzer
    from src.core.types import OrderSide, Signal, SignalStrength, StrategyType
    from src.indicators.technical import TechnicalIndicators

    ti = TechnicalIndicators()
    bars = [{"date": f"2026{m:02d}{d:02d}", "open": 100.0 + d, "high": 102.0 + d,
             "low": 99.0 + d, "close": 101.0 + d, "volume": 10000 + d}
            for m in (1, 2, 3, 4, 5, 6, 7, 8) for d in range(1, 29)]
    ti.calculate_all(SYM, bars)                       # 캐시 시각이 여기서 채워진다
    calc_at = ti._cache_ts[SYM]

    ba = _analyzer(monkeypatch, tmp_path)
    ba._screener = SimpleNamespace(_indicators=ti)
    sig = Signal(symbol=SYM, side=OrderSide.BUY, strength=SignalStrength.STRONG,
                 strategy=StrategyType.SEPA_TREND, price=Decimal("10000"),
                 stop_price=Decimal("9600"), target_price=Decimal("11000"), score=70.0,
                 reason="추세", metadata={"indicators": ti._cache[SYM]})
    p = BatchAnalyzer._to_pending_signal(ba, sig, now=NOW,
                                         expires=NOW + timedelta(hours=5), slippage_pct=3.0)
    assert p.inputs_ref["indicators_as_of"] == calc_at.isoformat()


def test_plan_reaches_signal_metadata_and_event(monkeypatch):
    """변환부가 signal.metadata['entry_plan'] 에 계획 전체를 싣고, 이벤트 직렬화에서 살아남는다."""
    from src.core.event import SignalEvent

    events = _run_execute(monkeypatch, [_plan()])
    assert len(events) == 1
    ev = events[0]
    plan = ev.signal.metadata["entry_plan"]
    assert plan["plan_id"] == "abc123def456"
    assert plan["max_entry_price"] == 10300.0
    assert plan["expires_at"] == _plan().expires_at
    assert plan["trigger"] == {"type": "none"}
    # event.metadata 는 별개 복사본이지만 계획은 양쪽에 있어야 한다
    assert ev.metadata["entry_plan"]["plan_id"] == "abc123def456"
    # 직렬화 왕복
    d = ev.to_dict() if hasattr(ev, "to_dict") else None
    if d is not None:
        assert json.loads(json.dumps(d, default=str))
    assert json.loads(json.dumps(plan))
    assert isinstance(ev, SignalEvent)


def test_signal_metadata_carries_quote_as_of(monkeypatch):
    """변환부가 현재가 **조회 시각**을 함께 싣는다 — shadow 신선도 게이트의 입력."""
    events = _run_execute(monkeypatch, [_plan()])
    q = events[0].signal.metadata["quote_as_of"]
    assert datetime.fromisoformat(q) == NOW     # 조회 시각 = 실행부 시계(동결)


def test_shadow_uses_quote_as_of_and_sees_price_reasons(home, monkeypatch):
    """호가 시각이 있으면 QUOTE_STALE 조기반환이 아니라 가격 계열 사유가 관측된다."""
    from test_risk_sizing import _em, _rm
    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "1")
    rm = _rm(monkeypatch, mode="nominal", em=_em())
    logs = _order_env(monkeypatch, rm)
    plan = _plan(max_entry_price=9000.0).to_dict()     # 현재가 10000 > 상한
    ev = _buy_signal(plan, quote_as_of=NOW.isoformat())   # 엔진 시계(동결) 기준 신선한 호가
    asyncio.run(rm.on_signal(ev))
    rec = next(r for r in logs if r["event_type"] == "shadow_plan_check")
    assert rec["metadata"]["status"] == "wait"
    assert "PRICE_ABOVE_CAP" in rec["metadata"]["reasons"]
    assert "QUOTE_STALE" not in rec["metadata"]["reasons"]
    assert rec["metadata"]["expected_fill_price"] == 10000.0


def _run_execute(monkeypatch, pending_list, quote_price=10100.0):
    """execute_pending_signals 를 합성 계획으로 1회 구동해 emit 된 이벤트를 수집."""
    ba = _analyzer(monkeypatch, None)
    ba._pending = list(pending_list)
    emitted = []

    class _FakeSigLog:
        async def log(self, **kw):
            return None

    import src.core.batch_analyzer as bam
    monkeypatch.setattr(bam._SigLog, "get", staticmethod(lambda: _FakeSigLog()))
    _freeze_clock(monkeypatch, bam)

    async def _emit(ev):
        emitted.append(ev)

    ba._engine = SimpleNamespace(
        portfolio=SimpleNamespace(positions={}), emit=_emit, _stock_name_cache={},
    )

    async def _quote_fn(_sym):
        return {"price": quote_price}

    async def _nxt():
        return set()

    ba._broker = SimpleNamespace(get_quote=_quote_fn, get_nxt_symbols=_nxt,
                                 get_positions=None)
    ba._sector_momentum = None
    ba._screener = SimpleNamespace(_indicators=SimpleNamespace(_cache={}))
    ba._config = {"batch": {"signal_interval_sec": 0}}
    ba._update_gap_down_tracking = lambda: None

    async def _premarket(sigs, _nxt_syms):
        return sigs

    ba._premarket_revalidate = _premarket
    asyncio.run(ba.execute_pending_signals())
    return emitted


# ── 3) 엔진 shadow 훅 (§4 #8·#10) ────────────────────────────────────────────

def _order_env(monkeypatch, rm):
    """on_signal 을 구동할 최소 스텁 (test_entry_risk_lifecycle 과 동일 패턴)."""
    from src.core.types import MarketSession
    logged = []

    class _FakeSigLog:
        async def log(self, **kw):
            logged.append(kw)

    import src.core.engine as eng
    monkeypatch.setattr(eng._SigLog, "get", staticmethod(lambda: _FakeSigLog()))
    _freeze_clock(monkeypatch, eng)

    rm._cross_validator = SimpleNamespace(
        validate=lambda **kw: (True, kw["score"], ""), last_memory_adj=0,
        last_llm_context={},
    )
    rm._risk_validator = None
    rm._sector_lookup = None
    rm._order_fail_cooldown = {}
    rm._COOLDOWN_SECONDS = 300
    rm._last_signal_time = {}
    rm._SIGNAL_COOLDOWN_SECONDS = 30
    rm._pending_orders = set()
    rm._pending_quantities = {}
    rm._pending_timestamps = {}
    rm._pending_sides = {}
    rm._pending_fallback_count = {}
    rm._pending_strategy = {}
    rm._pending_signal_cache = {}
    rm._pending_lock = asyncio.Lock()
    rm._last_cash_warn_time = None
    rm._LLM_CHECK_MIN, rm._LLM_BYPASS_AT, rm._LLM_REJECT_SIZE_MULT = 85, 95, 0.5
    rm._REPLACEMENT_MIN_SCORE = 85
    rm._check_factor_budget = lambda _s: None
    rm.engine.portfolio.positions = {}
    rm.engine.is_trading_hours = lambda: True
    rm.engine._get_current_session = lambda: MarketSession.REGULAR
    rm.engine.can_open_position = lambda *a, **k: (True, "")
    rm.engine._pending_sector_map = {}
    rm.engine._market_regime = "neutral"
    return logged


def _buy_signal(plan_dict=None, **extra):
    from test_risk_sizing import _sig
    meta = dict(extra)
    if plan_dict is not None:
        meta["entry_plan"] = plan_dict
    return _sig(2.5, **meta)


def test_shadow_hook_logs_plan_check(home, monkeypatch):
    from test_risk_sizing import _em, _rm
    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "1")
    rm = _rm(monkeypatch, mode="nominal", em=_em())
    logged = _order_env(monkeypatch, rm)
    ev = _buy_signal(_plan().to_dict())
    orders = asyncio.run(rm.on_signal(ev))

    assert orders, "shadow 검증은 주문을 막지 않는다"
    checks = [e for e in logged if e["event_type"] == "shadow_plan_check"]
    assert len(checks) == 1
    meta = checks[0]["metadata"]
    assert meta["plan_id"] == "abc123def456"
    # 엔진에는 호가 시각이 없다 → 정직하게 QUOTE_STALE (now 로 채우지 않는다)
    assert meta["status"] == "wait" and "QUOTE_STALE" in meta["reasons"]


def test_shadow_hook_skipped_without_plan(home, monkeypatch):
    """계획이 없으면 검증도 기록도 하지 않는다 (자동 생성 금지)."""
    from test_risk_sizing import _em, _rm
    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "1")
    rm = _rm(monkeypatch, mode="nominal", em=_em())
    logged = _order_env(monkeypatch, rm)
    assert asyncio.run(rm.on_signal(_buy_signal(None)))
    assert [e for e in logged if e["event_type"] == "shadow_plan_check"] == []


def test_shadow_hook_off_by_flag(home, monkeypatch):
    from test_risk_sizing import _em, _rm
    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "0")
    rm = _rm(monkeypatch, mode="nominal", em=_em())
    logged = _order_env(monkeypatch, rm)
    assert asyncio.run(rm.on_signal(_buy_signal(_plan().to_dict())))
    assert [e for e in logged if e["event_type"] == "shadow_plan_check"] == []


def test_checker_exception_does_not_stop_order(home, monkeypatch):
    """검증기 자체가 터져도 주문 경로는 계속된다 (§4 #10)."""
    from test_risk_sizing import _em, _rm
    import src.core.engine as eng
    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "1")

    def _boom(*a, **k):
        raise RuntimeError("검증기 폭발")

    monkeypatch.setattr(eng, "check_entry_plan", _boom)
    rm = _rm(monkeypatch, mode="nominal", em=_em())
    logged = _order_env(monkeypatch, rm)
    orders = asyncio.run(rm.on_signal(_buy_signal(_plan().to_dict())))
    assert orders and orders[0].order.quantity > 0
    assert [e for e in logged if e["event_type"] == "shadow_plan_check"] == []


def test_shadow_hook_reject_still_orders(home, monkeypatch):
    """reject 판정도 주문을 차단하지 않는다 — shadow 전용."""
    from test_risk_sizing import _em, _rm
    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "1")
    rm = _rm(monkeypatch, mode="nominal", em=_em())
    logged = _order_env(monkeypatch, rm)
    expired = _plan(expires_at=(NOW - timedelta(days=1)).isoformat()).to_dict()
    orders = asyncio.run(rm.on_signal(_buy_signal(expired)))
    assert orders
    chk = [e for e in logged if e["event_type"] == "shadow_plan_check"][0]["metadata"]
    assert chk["status"] == "reject" and "PLAN_EXPIRED" in chk["reasons"]


# ── 4) kr_scheduler 계약 (§4 #3 보유 indicators_as_of, 후보 dict 키) ──────────

_SAME = object()


def _sched_bot(pending=None, screened_at=None, stock_at=_SAME, indicators_at=None,
               candidate=None):
    """스케줄러 구동용 최소 bot.

    후보는 **운영 실제 타입**(`ScreenedStock`)으로 만든다. SimpleNamespace 로 필드명을
    지어내면 존재하지 않는 속성을 읽는 배선 누락이 테스트에 가려진다 (1차 리뷰 blocking 1:
    구현이 `current_price`/`entry_price` 만 봐서 운영에서 항상 None 이었다).
    지표 시각도 실제 출처인 `TechnicalIndicators._cache_ts` 에 넣는다.
    """
    from src.core.types import Position
    from src.indicators.technical import TechnicalIndicators
    from src.signals.screener.kr_screener import ScreenedStock
    from decimal import Decimal

    pos = Position(symbol=SYM, quantity=10, avg_price=Decimal("10000"),
                   current_price=Decimal("10500"), strategy="sepa_trend")
    if candidate is None:
        candidate = ScreenedStock(
            symbol=SYM, name="삼성전자", price=71000.0, change_pct=1.2, volume=100,
            score=72.0, screened_at=(screened_at if stock_at is _SAME else stock_at),
        )
    ti = TechnicalIndicators()
    ti._cache = {SYM: {"ma20": 9800}}
    ti._cache_ts = {} if indicators_at is None else {SYM: indicators_at}
    screener = SimpleNamespace(_indicators=ti)
    cand = candidate
    return SimpleNamespace(
        trading_team=None,
        _last_screened=[cand],
        _last_screened_at=screened_at,
        batch_analyzer=SimpleNamespace(_screener=screener, _pending=list(pending or [])),
        engine=SimpleNamespace(portfolio=SimpleNamespace(positions={SYM: pos}),
                               risk_manager=None, _market_regime="neutral"),
        risk_manager=None,
    )


def _collect_deliberations(monkeypatch, bot, slot="10:30"):
    """_run_team_deliberation_once 를 구동해 team.deliberate_many 인자를 수집."""
    from src.schedulers.kr_scheduler import KRScheduler
    calls = []

    class _Team:
        async def deliberate_many(self, items, holding=False, **kw):
            calls.append({"items": items, "holding": holding, "kw": kw})
            return []

    bot.trading_team = _Team()
    sched = object.__new__(KRScheduler)
    sched.bot = bot
    asyncio.run(sched._run_team_deliberation_once(slot=slot))
    return calls


def test_holding_dict_carries_indicators_as_of(monkeypatch):
    """보유 재평가는 지표 캐시의 **실제 계산 시각**(_cache_ts)을 넘긴다 (C3)."""
    at = datetime(2026, 9, 15, 10, 25)
    ind_at = datetime(2026, 9, 15, 8, 40)
    calls = _collect_deliberations(
        monkeypatch, _sched_bot(screened_at=at, indicators_at=ind_at))
    holding = next(c for c in calls if c["holding"])["items"][0]
    assert holding["indicators_as_of"] == ind_at


def test_holding_indicators_as_of_is_none_when_unknown(monkeypatch):
    """출처가 없으면 now 로 채우지 않는다 — 신선도 세탁 금지 (§4 #3)."""
    calls = _collect_deliberations(monkeypatch, _sched_bot(screened_at=None))
    holding = next(c for c in calls if c["holding"])["items"][0]
    assert holding["indicators_as_of"] is None


def test_candidate_dict_key_contract(monkeypatch):
    """ScreenedStock 의 실제 가격 필드(`price`)·시각(`screened_at`)을 읽는다."""
    at = datetime(2026, 9, 15, 10, 25)
    plan = _plan()
    calls = _collect_deliberations(monkeypatch, _sched_bot(pending=[plan], screened_at=at),
                                   slot="10:30")
    cand = next(c for c in calls if not c["holding"])["items"][0]
    assert cand["slot"] == "10:30"
    assert cand["entry_plan"]["plan_id"] == "abc123def456"
    assert cand["current_price"] == 71000.0
    assert cand["quote_as_of"] == at


def test_candidate_quote_as_of_prefers_stock_time(monkeypatch):
    """종목별 실측 시각이 사이클 시각보다 우선한다 (사이클 시각은 폴백)."""
    cycle_at = datetime(2026, 9, 15, 10, 25)
    stock_at = datetime(2026, 9, 15, 10, 21)
    calls = _collect_deliberations(
        monkeypatch, _sched_bot(screened_at=cycle_at, stock_at=stock_at))
    cand = next(c for c in calls if not c["holding"])["items"][0]
    assert cand["quote_as_of"] == stock_at


def test_candidate_swing_candidate_price(monkeypatch):
    """SwingCandidate(가격 필드가 entry_price, 시각 없음)도 그대로 배선된다."""
    from decimal import Decimal
    from src.signals.screener.swing_screener import SwingCandidate

    cycle_at = datetime(2026, 9, 15, 10, 25)
    sc = SwingCandidate(symbol=SYM, name="삼성전자", strategy="sepa_trend", score=70.0,
                        entry_price=Decimal("10000"), stop_price=Decimal("9500"),
                        target_price=Decimal("11000"), indicators={"rs_rating": 88})
    calls = _collect_deliberations(
        monkeypatch, _sched_bot(screened_at=cycle_at, candidate=sc))
    cand = next(c for c in calls if not c["holding"])["items"][0]
    assert cand["current_price"] == 10000.0
    assert cand["quote_as_of"] == cycle_at


def test_candidate_without_pending_plan_gets_none(monkeypatch):
    calls = _collect_deliberations(
        monkeypatch, _sched_bot(pending=[], screened_at=None, stock_at=None))
    cand = next(c for c in calls if not c["holding"])["items"][0]
    assert cand["entry_plan"] is None
    assert cand["quote_as_of"] is None


# ── FINAL 리뷰 후속 (2026-09-15) ─────────────────────────────────────────────

def test_unreadable_plan_input_waits_instead_of_allowing():
    """문자열·빈 객체 등 계획으로 읽을 수 없는 입력은 '조건 없음 = 허용' 으로 떨어지지 않는다."""
    now = datetime.now()
    quote = {"price": 10000.0, "as_of": now.isoformat()}
    for bad in ("not-a-plan", 42, object(), {}):
        chk = check_entry_plan(bad, quote, now)
        assert chk.status == "wait", bad
        assert "INPUT_MISSING:plan" in chk.reasons


@pytest.mark.parametrize("as_of_offset, expect_level", [
    (timedelta(0), "severe"),            # 오늘 갱신 → 그대로 전달
    (timedelta(days=-1), None),          # 어제 상태 → 오늘 관측처럼 넘기지 않는다
    (None, None),                        # 갱신 시각 모름 → 전달 안 함
])
def test_shadow_hook_passes_intraday_level_only_when_updated_today(home, monkeypatch,
                                                                   as_of_offset, expect_level):
    from test_risk_sizing import _em, _rm
    from src.core import engine as engine_mod
    monkeypatch.setenv("ENTRY_PLAN_SHADOW", "1")
    rm = _rm(monkeypatch, mode="nominal", em=_em())
    _order_env(monkeypatch, rm)
    seen = {}

    def _fake_check(plan, quote, now, **kw):
        seen.update(kw)
        return PlanCheck(status="allow", reasons=[], checked_at=now)
    monkeypatch.setattr(engine_mod, "check_entry_plan", _fake_check)

    extra = {"intraday_state": "severe"}
    if as_of_offset is not None:
        extra["intraday_state_as_of"] = (NOW + as_of_offset).isoformat()   # 엔진 시계(동결) 기준
    asyncio.run(rm.on_signal(_buy_signal(_plan().to_dict(), **extra)))
    assert seen.get("intraday_level") == expect_level


def test_signal_event_shadow_rows_do_not_reach_sse_feed():
    """shadow_plan_check 행은 DB 에만 남고 실시간(SSE) 피드로 나가지 않는다 — 대시보드 '감점' 오독 방지."""
    from src.data.storage.signal_event_storage import SignalEventStorage

    class _Conn:
        async def fetchrow(self, *_a, **_k):
            return {"id": 1, "event_time": datetime.now()}
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    class _Pool:
        def acquire(self): return _Conn()

    st = SignalEventStorage.__new__(SignalEventStorage)
    st._pool = _Pool()
    st._sse_callback = lambda *_a, **_k: None
    pushed = []

    async def _ensure_init(): return None
    async def _push(row_id, event_time, **kw): pushed.append(kw["event_type"])
    st._ensure_init = _ensure_init
    st._push_sse = _push

    base = dict(symbol="005930", name="삼성전자", strategy="sepa_trend", score=70.0,
                adjusted_score=70.0, side="buy", market_regime="neutral", sector=None, metadata={})
    asyncio.run(st._write(event_type="shadow_plan_check", **base))
    asyncio.run(st._write(event_type="blocked", **base))
    asyncio.run(st._write(event_type="passed", **base))
    assert pushed == ["blocked", "passed"]


def test_signal_event_stats_and_default_listing_exclude_shadow_rows():
    """총계·전략별·일별 집계와 type 미지정 기본 조회의 SQL 이 실제 판정 행만 센다(정적 확인)."""
    import inspect
    from src.data.storage import signal_event_storage as m
    stats_src = inspect.getsource(m.SignalEventStorage.get_stats)
    assert stats_src.count("event_type IN ('passed','blocked','penalized')") >= 3
    recent_src = inspect.getsource(m.SignalEventStorage.get_recent)
    assert "event_type IN ('passed','blocked','penalized')" in recent_src


def test_ledger_does_not_mask_numeric_only_identifiers(tmp_path):
    """plan_id/deliberation_id 가 우연히 전부 숫자여도 '***' 로 지워지지 않는다(원장 조인 보호)."""
    from src.agents import team_ledger
    row = {"plan_id": "1234567890123456", "deliberation_id": "9876543210987654",
           "note": "계좌 12345678-01 은 가린다", "api_key": "sk-abcdefghijklmnopqrstuvwxyz"}
    out = team_ledger._mask_secrets(row)
    assert out["plan_id"] == "1234567890123456"
    assert out["deliberation_id"] == "9876543210987654"
    assert "***" in out["note"] and "12345678-01" not in out["note"]
    assert out["api_key"] == "***"
