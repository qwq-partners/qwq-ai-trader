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
    ("sepa_trend", "breakout", "vcp_breakout"),
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
                           "indicators": {"rs_rating": 88, "as_of": "2026-09-15T09:00:00"}})
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
    assert set(p.inputs_ref["indicator_keys"]) == {"rs_rating", "as_of"}
    assert p.inputs_ref["indicators_as_of"] == "2026-09-15T09:00:00"
    # 계획 id 는 후보마다 다르다
    p2 = BatchAnalyzer._to_pending_signal(ba, sig, now=NOW,
                                          expires=NOW + timedelta(hours=5), slippage_pct=3.0)
    assert p2.plan_id != p.plan_id


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

def _sched_bot(pending=None, screened_at=None):
    from src.core.types import Position
    from decimal import Decimal

    pos = Position(symbol=SYM, quantity=10, avg_price=Decimal("10000"),
                   current_price=Decimal("10500"), strategy="sepa_trend")
    cand = SimpleNamespace(symbol=SYM, name="삼성전자", strategy="sepa_trend", score=70.0,
                           entry_price=Decimal("10000"), sector="반도체",
                           indicators={"rs_rating": 88})
    screener = SimpleNamespace(_indicators=SimpleNamespace(_cache={SYM: {"ma20": 9800}}))
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
    at = datetime(2026, 9, 15, 10, 25)
    calls = _collect_deliberations(monkeypatch, _sched_bot(screened_at=at))
    holding = next(c for c in calls if c["holding"])["items"][0]
    assert "indicators_as_of" in holding, "보유 재평가도 지표 시각을 넘겨야 한다 (C3)"


def test_holding_indicators_as_of_is_none_when_unknown(monkeypatch):
    """출처가 없으면 now 로 채우지 않는다 — 신선도 세탁 금지 (§4 #3)."""
    calls = _collect_deliberations(monkeypatch, _sched_bot(screened_at=None))
    holding = next(c for c in calls if c["holding"])["items"][0]
    assert holding["indicators_as_of"] is None


def test_candidate_dict_key_contract(monkeypatch):
    at = datetime(2026, 9, 15, 10, 25)
    plan = _plan()
    calls = _collect_deliberations(monkeypatch, _sched_bot(pending=[plan], screened_at=at),
                                   slot="10:30")
    cand = next(c for c in calls if not c["holding"])["items"][0]
    assert cand["slot"] == "10:30"
    assert cand["entry_plan"]["plan_id"] == "abc123def456"
    assert cand["current_price"] == 10000.0
    assert cand["quote_as_of"] == at
    assert cand["indicators_as_of"] == at


def test_candidate_without_pending_plan_gets_none(monkeypatch):
    calls = _collect_deliberations(monkeypatch, _sched_bot(pending=[], screened_at=None))
    cand = next(c for c in calls if not c["holding"])["items"][0]
    assert cand["entry_plan"] is None
    assert cand["quote_as_of"] is None
