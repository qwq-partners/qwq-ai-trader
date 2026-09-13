"""진입 위험 스냅샷 생명주기 (계획서 T3 / 결함 F4, 2026-09-14)

기준 결함(F4): 09-13 risk 태그(sizing_mode/risk_stop_pct/stop_source/stop_crash_active)가 원본
`Signal.metadata` 에만 있고 **별개 dict 인 `event.metadata`** · 주문 캐시(`_pending_signal_cache`) ·
signal_events 로그 · 체결 원장(`market_context`) 에는 없어 canary 원장에서 risk 모드 체결을 골라낼 수 없다.

여기서 검증하는 왕복:
    from_signal → 엔진 사이징(스냅샷 생성) → event/signal 양쪽 메타 → on_signal 주문 캐시·signal_events
    → record_entry(market_context) → TradeStorage(JSONB mock)/TradeJournal(JSON) 저장·복원
    → ExitManager 영속 상태(체결 확정 R 분모) → scripts/export_risk_ledger → review_risk_canary CLI

프로덕션 캐시·네트워크·DB 무접촉 — Path.home() 은 tmp_path, DB 는 mock, 합성 값만 사용.
실행: venv/bin/python -m pytest tests/test_entry_risk_lifecycle.py -q -p no:cacheprovider
"""
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from src.core.types import MarketSession, OrderSide, Position, StrategyType  # noqa: E402
from src.strategies.exit_manager import ExitConfig, ExitManager  # noqa: E402
from src.utils.entry_risk import (  # noqa: E402
    ENTRY_RISK_REQUIRED, applied_sha, build_entry_risk_snapshot, confirm_initial_risk,
    effective_config_hash, planned_vs_filled_delta,
)
from src.utils.fee_calculator import get_fee_calculator  # noqa: E402
from src.utils.stop_policy import StopDecision  # noqa: E402

# T2 픽스처 재사용 (같은 모듈 객체 — pytest 가 이미 임포트한 것)
from test_risk_sizing import (  # noqa: E402
    EQ, PRICE, STRATEGY_EXIT_PARAMS, SYM, _em, _rm, _sig, home,  # noqa: F401
)

def _load_script(name):
    """scripts/ 는 패키지가 아니므로 파일 경로로 로드 (tests/test_risk_canary_report.py 와 동일 패턴)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"_{name}_for_test", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


exporter = _load_script("export_risk_ledger")
canary = _load_script("review_risk_canary")

FEE = get_fee_calculator("KR")
SHA = "a" * 40
CFG_HASH = "c" * 64


def _decision(stop="5.0", source="strategy", crash=False):
    return StopDecision(Decimal(stop), source, crash)


def _snapshot(quantity=139, **kw):
    kw.setdefault("strategy", "sepa_trend")
    kw.setdefault("stop_decision", _decision())
    kw.setdefault("equity", EQ)
    kw.setdefault("price", PRICE)
    kw.setdefault("risk_per_trade_pct", 0.7)
    kw.setdefault("fee_calc", FEE)
    kw.setdefault("applied_sha", SHA)
    kw.setdefault("config_hash", CFG_HASH)
    kw.setdefault("signal_ts", "2026-09-14T10:30:00+09:00")
    return build_entry_risk_snapshot(quantity=quantity, **kw)


# ── 순수 함수 계약 ─────────────────────────────────────────────────────────────

def test_snapshot_contract_keys_and_types():
    snap = _snapshot()
    assert set(ENTRY_RISK_REQUIRED) <= set(snap)
    assert snap["version"] == 1
    assert snap["cohort_id"] == "risk-sepa_trend-v1"
    assert snap["sizing_mode"] == "risk"
    assert snap["stop_basis"] == "net_pnl"
    assert (snap["stop_pct"], snap["stop_source"], snap["stop_crash_active"]) == ("5.0", "strategy", False)
    assert snap["equity_at_decision"] == "10000000"
    assert snap["risk_budget_amount"] == "70000.0"
    assert snap["planned_price"] == "10000"
    assert snap["planned_quantity"] == 139
    assert snap["planned_risk_amount"] == "69509.75"   # 매수수수료 포함 entry_cost × 5%
    assert (snap["applied_sha"], snap["config_hash"]) == (SHA, CFG_HASH)
    assert snap["signal_ts"].startswith("2026-09-14T")
    # JSON 직렬화 가능 (Decimal 객체가 남아 있으면 실패)
    assert json.loads(json.dumps(snap)) == snap


def test_snapshot_is_json_only_and_decimal_is_string():
    snap = _snapshot(quantity=2, price=Decimal("500000"))
    assert isinstance(snap["planned_risk_amount"], str)
    assert isinstance(snap["planned_quantity"], int)
    assert Decimal(snap["planned_risk_amount"]) == Decimal("50007.05")


def test_config_hash_excludes_credentials():
    base = {"risk": {"sizing_mode": "risk", "risk_per_trade_pct": 0.7},
            "kr": {"strategies": {"sepa_trend": {"stop_loss_pct": 5.0}}}}
    secret = dict(base)
    secret.update({
        "KIS_APPKEY": "PSxxxx", "KIS_APPSECRET": "ssss", "KIS_CANO": "12345678",
        "openai_api_key": "sk-live", "telegram": {"bot_token": "123:abc", "chat_id": 99},
        "db_url": "postgres://user:pw@host/db",
    })
    assert effective_config_hash(base) == effective_config_hash(secret)
    # 위험 설정이 바뀌면 hash 도 바뀐다 (계측 의미 유지)
    changed = {"risk": {"sizing_mode": "risk", "risk_per_trade_pct": 1.0},
               "kr": base["kr"]}
    assert effective_config_hash(changed) != effective_config_hash(base)
    assert len(effective_config_hash(base)) == 64


def test_confirm_initial_risk_accumulates_partial_fills():
    fills = [{"price": "10000", "quantity": 100, "fee": "140"},
             {"price": "10050", "quantity": 39, "fee": "55"}]
    initial, cost = confirm_initial_risk(fills, Decimal("5.0"), fee_calc=FEE)
    assert cost == Decimal("1391950")                  # 10000×100 + 10050×39
    assert initial == Decimal("69597.50")              # cost × 5%
    planned = Decimal(_snapshot()["planned_risk_amount"])
    assert planned_vs_filled_delta(planned, initial) == initial - planned


def test_confirm_initial_risk_rejects_invalid_stop():
    with pytest.raises(ValueError):
        confirm_initial_risk([{"price": "1", "quantity": 1, "fee": "0"}], Decimal("0"))


def test_applied_sha_is_cached(monkeypatch):
    import src.utils.entry_risk as er
    calls = []
    monkeypatch.setattr(er, "_APPLIED_SHA", None)
    monkeypatch.setattr(er.subprocess, "check_output",
                        lambda *a, **k: calls.append(a) or "deadbeef\n")
    assert applied_sha() == "deadbeef"
    assert applied_sha() == "deadbeef"
    assert len(calls) == 1


def test_applied_sha_unknown_on_failure(monkeypatch):
    import src.utils.entry_risk as er

    def _boom(*a, **k):
        raise OSError("git 없음")

    monkeypatch.setattr(er, "_APPLIED_SHA", None)
    monkeypatch.setattr(er.subprocess, "check_output", _boom)
    assert applied_sha() == "unknown"


# ── 엔진: 이벤트 복사본 양쪽에 스냅샷 ──────────────────────────────────────────

def test_snapshot_lands_in_both_metadata_copies(home, monkeypatch):
    rm = _rm(monkeypatch, mode="risk", em=_em())
    sig = _sig(2.5)
    assert sig.metadata is not sig.signal.metadata      # 기존 계약: 별개 복사본
    assert rm._calculate_position_size(sig) == 139

    for meta in (sig.metadata, sig.signal.metadata):
        snap = meta["entry_risk"]
        assert snap["sizing_mode"] == "risk"
        assert snap["planned_quantity"] == 139
        assert snap["stop_pct"] == "5.0"
        assert snap["cohort_id"] == "risk-sepa_trend-v1"
    # 별개 복사본 — 한쪽 변경이 다른 쪽을 오염시키지 않는다
    assert sig.metadata["entry_risk"] is not sig.signal.metadata["entry_risk"]
    sig.metadata["entry_risk"]["planned_quantity"] = 1
    assert sig.signal.metadata["entry_risk"]["planned_quantity"] == 139
    # 기존 태그 유지
    assert sig.signal.metadata["risk_stop_pct"] == 5.0


def test_rejected_quantity_leaves_no_snapshot(home, monkeypatch):
    # 2주 × 50만 = 100만 < 최소 120만 → 거부 (주문 없는 태그 방지)
    rm = _rm(monkeypatch, mode="risk", em=_em(), min_value=1200000)
    sig = _sig(2.5, price=Decimal("500000"))
    assert rm._calculate_position_size(sig) == 0
    assert "entry_risk" not in sig.metadata
    assert "entry_risk" not in sig.signal.metadata


def test_nominal_and_core_have_no_snapshot(home, monkeypatch):
    rm = _rm(monkeypatch, mode="nominal", em=_em())
    sig = _sig(2.5)
    assert rm._calculate_position_size(sig) == 239
    assert "entry_risk" not in sig.metadata and "entry_risk" not in sig.signal.metadata

    rm2 = _rm(monkeypatch, mode="risk", em=_em())
    core = _sig(2.5, StrategyType.CORE_HOLDING)
    assert rm2._calculate_position_size(core) == 95
    assert "entry_risk" not in core.metadata


def test_snapshot_planned_risk_within_budget(home, monkeypatch):
    rm = _rm(monkeypatch, mode="risk", em=_em(), overlays={"calendar": 1.1, "team": 1.2})
    sig = _sig(2.5)
    q = rm._calculate_position_size(sig)
    snap = sig.metadata["entry_risk"]
    assert snap["planned_quantity"] == q
    assert Decimal(snap["planned_risk_amount"]) <= Decimal(snap["risk_budget_amount"])


# ── 주문 경로: 주문 캐시·signal_events 로그 ────────────────────────────────────

def _order_path(monkeypatch, rm, sig):
    """실제 on_signal(BUY) 로 주문 캐시·_log_sig 까지 구동. 게이트는 통과 스텁."""
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

    orders = asyncio.run(rm.on_signal(sig))
    return orders, logged


def test_order_cache_and_sig_log_carry_snapshot(home, monkeypatch):
    rm = _rm(monkeypatch, mode="risk", em=_em())
    sig = _sig(2.5)
    orders, logged = _order_path(monkeypatch, rm, sig)

    assert orders and orders[0].order.quantity == 139
    cached = rm._pending_signal_cache[SYM]["metadata"]
    assert cached["entry_risk"]["planned_quantity"] == 139
    assert cached["entry_risk"]["cohort_id"] == "risk-sepa_trend-v1"

    passed = [e for e in logged if e["event_type"] == "passed"]
    assert passed and passed[0]["metadata"]["entry_risk"]["sizing_mode"] == "risk"


# ── 체결 원장 저장·복원 (TradeStorage JSONB mock + TradeJournal JSON) ──────────

def _journal(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_JOURNAL_DIR", str(tmp_path / "journal"))
    from src.core.evolution.trade_journal import TradeJournal
    return TradeJournal()


def test_journal_record_entry_roundtrips_snapshot(tmp_path, monkeypatch):
    from src.core.evolution.trade_journal import TradeJournal, TradeRecord
    snap = _snapshot()
    j = _journal(tmp_path, monkeypatch)
    rec = j.record_entry(
        trade_id="T1", symbol=SYM, name="삼성전자", entry_price=10000, entry_quantity=139,
        entry_reason="돌파", entry_strategy="sepa_trend",
        market_context={"regime": "bull", "entry_risk": snap},
        entry_tags=["a", "b", "c"],
    )
    assert rec.market_context["entry_risk"] == snap
    # 파일 → from_dict 복원
    saved = json.loads(next((tmp_path / "journal").glob("trades_*.json")).read_text(encoding="utf-8"))
    assert saved["trades"][0]["market_context"]["entry_risk"] == snap
    restored = TradeRecord.from_dict(saved["trades"][0])
    assert restored.market_context["entry_risk"]["planned_risk_amount"] == snap["planned_risk_amount"]
    # 새 저널 인스턴스가 디스크에서 읽어도 동일
    j2 = TradeJournal()
    assert j2.get_trade("T1").market_context["entry_risk"] == snap
    assert isinstance(j2, TradeJournal)


def test_trade_storage_enqueues_snapshot_in_jsonb(tmp_path, monkeypatch):
    from src.data.storage.trade_storage import TradeStorage
    snap = _snapshot()
    monkeypatch.setenv("TRADE_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("DATABASE_URL", "")
    storage = TradeStorage(db_url="")
    enqueued = []
    monkeypatch.setattr(storage, "_enqueue", lambda sql, params: enqueued.append((sql, params)))
    storage.record_entry(
        trade_id="T2", symbol=SYM, name="삼성전자", entry_price=10000, entry_quantity=139,
        entry_reason="돌파", entry_strategy="sepa_trend",
        market_context={"entry_risk": snap}, entry_tags=["a", "b", "c"],
    )
    sql, params = enqueued[0]
    assert "INSERT INTO trades" in sql
    market_context_json = params[10]                      # $11 market_context
    assert json.loads(market_context_json)["entry_risk"] == snap


# ── ExitManager: 확정 R 분모 영속 (재시작·부분매도·레짐 변경 불변) ───────────────

def _register(em, qty=139, price=PRICE):
    pos = Position(symbol=SYM, quantity=qty, avg_price=price, current_price=price,
                   strategy="sepa_trend")
    p = STRATEGY_EXIT_PARAMS["sepa_trend"]
    em.register_position(pos, stop_loss_pct=p["stop_loss_pct"],
                         trailing_stop_pct=p["trailing_stop_pct"], atr_pct_hint=2.5)
    return pos


def test_initial_risk_persisted_and_restored(home):
    em = _em()
    _register(em)
    em.set_initial_risk(SYM, Decimal("69500"), Decimal("5.0"))
    st = em.get_state(SYM)
    assert (st.initial_risk_amount, st.actual_stop_pct) == (Decimal("69500"), Decimal("5.0"))

    # 재시작: 새 인스턴스가 stage 파일에서 복원
    em2 = _em()
    _register(em2)
    st2 = em2.get_state(SYM)
    assert st2.initial_risk_amount == Decimal("69500")
    assert st2.actual_stop_pct == Decimal("5.0")

    # 부분 매도·레짐 변경·재등록에도 분모 불변 (덮어쓰기 금지)
    em2.set_initial_risk(SYM, Decimal("1"), Decimal("9.9"))
    assert em2.get_state(SYM).initial_risk_amount == Decimal("69500")
    em2.apply_regime_params("bear")
    _register(em2, qty=125)
    assert em2.get_state(SYM).initial_risk_amount == Decimal("69500")
    assert em2.get_state(SYM).actual_stop_pct == Decimal("5.0")


def test_states_without_initial_risk_stay_none(home):
    em = _em()
    _register(em)
    assert em.get_state(SYM).initial_risk_amount is None
    em2 = _em()
    _register(em2)
    assert em2.get_state(SYM).initial_risk_amount is None      # 구 스키마 호환


# ── exporter → canary CLI 왕복 ─────────────────────────────────────────────────

def _closed_trade(tmp_path, monkeypatch, *, with_snapshot=True, trade_id="T1"):
    j = _journal(tmp_path, monkeypatch)
    ctx = {"regime": "bull"}
    if with_snapshot:
        ctx["entry_risk"] = _snapshot()
    j.record_entry(trade_id=trade_id, symbol=SYM, name="삼성전자", entry_price=10000,
                   entry_quantity=139, entry_reason="돌파", entry_strategy="sepa_trend",
                   market_context=ctx, entry_tags=["a", "b", "c"])
    j.record_exit(trade_id=trade_id, exit_price=10500, exit_quantity=139,
                  exit_reason="트레일링", exit_type="trailing")
    return j


def test_exporter_ledger_passes_canary(tmp_path, monkeypatch, home):
    build_ledger = exporter.build_ledger
    j = _closed_trade(tmp_path, monkeypatch)
    em = _em()
    _register(em)
    em.set_initial_risk(SYM, Decimal("69500"), Decimal("5.0"))

    ledger = build_ledger(list(j._trades.values()), em._persisted, applied_sha=SHA)
    pos = ledger["positions"][0]
    assert pos["status"] == "closed"
    assert pos["cohort_id"] == "risk-sepa_trend-v1"
    assert Decimal(pos["initial_risk_amount"]) == Decimal("69500")
    assert Decimal(pos["actual_stop_pct"]) == Decimal("5.0")
    assert pos["lots_ambiguous"] is False
    assert Decimal(pos["planned_vs_filled_risk_delta"]) == Decimal("69500") - Decimal("69509.75")

    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    canary_main = canary.main
    out = tmp_path / "report.json"
    rc = canary_main(["--input", str(ledger_path), "--cohort", "risk-sepa_trend-v1",
                      "--output", str(out), "--sha", SHA])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["technical_status"] == "passed", report.get("technical_issues")
    assert report["sample"]["closed"] == 1


def test_exporter_marks_legacy_and_ambiguous(tmp_path, monkeypatch, home):
    build_ledger = exporter.build_ledger
    j = _closed_trade(tmp_path, monkeypatch, with_snapshot=False, trade_id="LEGACY")
    ledger = build_ledger(list(j._trades.values()), {}, applied_sha=SHA)
    pos = ledger["positions"][0]
    assert pos["entry_risk"] is None
    assert pos["initial_risk_amount"] is None
    assert pos["cohort_id"] == "legacy-unmeasured"

    # 같은 종목의 보유 구간이 겹치면 lot 구분 불가
    j2 = _journal(tmp_path / "amb", monkeypatch)
    for tid in ("A", "B"):
        j2.record_entry(trade_id=tid, symbol=SYM, name="삼성전자", entry_price=10000,
                        entry_quantity=10, entry_reason="돌파", entry_strategy="sepa_trend",
                        market_context={"entry_risk": _snapshot(quantity=10)},
                        entry_tags=["a", "b", "c"])
    ledger2 = build_ledger(list(j2._trades.values()), {}, applied_sha=SHA)
    assert all(p["lots_ambiguous"] is True for p in ledger2["positions"])
    assert all(p["status"] == "open" for p in ledger2["positions"])


def test_exporter_cli_writes_ledger(tmp_path, monkeypatch, home):
    export_main = exporter.main
    _closed_trade(tmp_path, monkeypatch)
    out = tmp_path / "out.json"
    rc = export_main(["--source", "journal", "--output", str(out), "--days", "30"])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["version"] == 1 and len(data["positions"]) == 1
