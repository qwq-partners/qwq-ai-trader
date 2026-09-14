"""진입 위험 스냅샷의 체결 경로 배선 (계획서 T3 — A 담당, 2026-09-14)

대상: `KRScheduler.run_fill_check()` 의 BUY 체결 분기 —
      `confirm_initial_risk` → `ExitManager.set_initial_risk` → `merge_confirmed_risk`
      → `record_entry(market_context["entry_risk"])`.

`tests/test_sync_portfolio_characterization.py` 의 가짜 협력자(`_make`/`_pos`/`_fill_check_once`/
`_buy_fill`)를 그대로 import 해서 쓴다(그 파일은 수정하지 않는다). 브로커·ExitManager·저널은
전부 기록용 가짜 — 네트워크·DB·운영 캐시 무접촉.

실행: venv/bin/python -m pytest tests/test_entry_risk_wiring.py -q
"""

import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from src.core.types import Fill, OrderSide  # noqa: E402
from src.utils.entry_risk import build_entry_risk_snapshot  # noqa: E402
from src.utils.fee_calculator import get_fee_calculator  # noqa: E402
from src.utils.stop_policy import StopDecision  # noqa: E402

from test_sync_portfolio_characterization import (  # noqa: E402
    _buy_fill, _fail_n_times, _fill_check_once, _make, _pos,
)
from test_entry_risk_lifecycle import _load_script  # noqa: E402

exporter = _load_script("export_risk_ledger")
canary = _load_script("review_risk_canary")

FEE = get_fee_calculator("KR")
SYM = "005930"
SHA = "a" * 40
# T2 인수 조건: equity 1천만 · 가격 1만원 · net SL 5% · 위험률 0.7% → 139주
PRICE = Decimal("10000")
QTY = 139
STOP = Decimal("5.0")


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """운영 캐시(~/.cache/ai_trader) 무접촉 — ExitManager 생성자·체결 경로의 레짐 캐시 조회가
    실제 HOME 을 읽던 누출을 tests/conftest.py 격리 가드가 잡았다 (2026-09-15).
    HOME 환경변수는 건드리지 않고 Path.home() 만 이 테스트 프로세스 안에서 바꾼다."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def _snapshot(quantity=QTY, stop="5.0", price=PRICE):
    return build_entry_risk_snapshot(
        strategy="sepa_trend",
        stop_decision=StopDecision(Decimal(stop), "strategy", False),
        equity=Decimal("10000000"), price=price, quantity=quantity,
        risk_per_trade_pct=0.7, fee_calc=FEE, applied_sha=SHA,
        config_hash="c" * 64, signal_ts="2026-09-14T10:30:00+09:00",
    )


class _Order:
    """브로커 인메모리 미체결 주문 (id 만 쓰인다)."""

    def __init__(self, order_id):
        self.id = order_id


class _Journal:
    """record_entry / get_trade / update_market_context 만 가진 가짜 저널."""

    def __init__(self):
        self.entries = []          # record_entry kwargs
        self.updates = []          # (trade_id, patch)
        self.records = {}          # trade_id → SimpleNamespace(market_context)

    def record_entry(self, **kw):
        self.entries.append(kw)
        rec = SimpleNamespace(id=f"T_{len(self.entries)}",
                              market_context=dict(kw.get("market_context") or {}))
        self.records[rec.id] = rec
        return rec

    def get_trade(self, trade_id):
        return self.records.get(trade_id)

    def update_market_context(self, trade_id, patch):
        self.updates.append((trade_id, patch))
        rec = self.records.get(trade_id)
        if rec is not None:
            rec.market_context.update(patch)
        return True


class _ExitManagerRisk:
    """set_initial_risk / resolve_stop 를 기록하는 가짜 ExitManager."""

    def __init__(self, stop=STOP, raise_on_resolve=False):
        self._exit_exempt = set()
        self.registered = []
        self.removed = []
        self.initial_risks = []        # (symbol, amount, stop_pct)
        self._stop = stop
        self._raise = raise_on_resolve

    def register_position(self, position, **kwargs):
        self.registered.append((position, kwargs))

    def remove_position(self, symbol):
        self.removed.append(symbol)
        return True

    def is_exit_exempt(self, symbol):
        return symbol in self._exit_exempt

    def resolve_stop(self, *, dynamic_stop_pct, fixed_stop_pct, is_core, apply_crash_cap=True):
        if self._raise:
            raise ValueError("유효하지 않은 손절 설정")
        return StopDecision(self._stop, "strategy", False)

    def set_initial_risk(self, symbol, amount, stop_pct):
        if any(s == symbol for s, _, _ in self.initial_risks):
            return False       # 최초 1회 (멱등)
        self.initial_risks.append((symbol, Decimal(str(amount)), Decimal(str(stop_pct))))
        return True


def _setup(monkeypatch, *, snapshot=None, stop=STOP, raise_on_resolve=False,
           positions=None, strategy="sepa_trend"):
    """BUY 체결 경로만 구동하는 최소 bot/scheduler."""
    pos = _pos(SYM, qty=QTY, avg=str(PRICE), cur=str(PRICE), strategy=strategy)
    sched, bot, sleeps = _make(
        monkeypatch,
        bot_positions=[pos] if positions is None else positions,
        balance={}, kis_seq=[],
        exit_params={"sepa_trend": {"stop_loss_pct": 5.0}, "_sync": {"stop_loss_pct": 3.0}},
    )
    bot.exit_manager = _ExitManagerRisk(stop=stop, raise_on_resolve=raise_on_resolve)
    bot.trade_journal = _Journal()
    meta = {"atr_pct": 1.5}
    if snapshot is not None:
        meta["entry_risk"] = snapshot
    bot.engine.risk_manager._pending_signal_cache = {
        SYM: {"reason": "돌파", "reasons": ["20일 신고가"], "score_breakdown": {},
              "context_snapshot": {}, "metadata": meta, "strategy": "sepa_trend", "score": 82.0},
    }
    bot.broker.open_orders_seq = []
    return sched, bot, sleeps


def _drive(monkeypatch, sched, bot, sleeps, fills, open_after):
    """한 주기 구동 — check_fills 가 `fills` 를 주고, 이후 미체결 주문은 `open_after`."""
    bot.broker.fills_seq = [fills]
    bot.broker.get_open_orders = _open_orders_fn(bot, open_after)
    _fill_check_once(monkeypatch, sched, sleeps)


def _open_orders_fn(bot, open_after):
    """1회차(fills 존재 확인)는 미체결 있음, check_fills 이후는 `open_after`."""
    state = {"n": 0}

    async def _get():
        state["n"] += 1
        if state["n"] == 1:
            return [_Order("o-x")] if bot.broker.fills_seq else []
        return [_Order(oid) for oid in open_after]

    return _get


def _entry_risk_of(journal):
    ctx = journal.entries[-1]["market_context"] or {}
    return ctx.get("entry_risk")


# ── 1. 단일 체결로 완결 → 즉시 확정 ─────────────────────────────────────────────

def test_single_fill_confirms_initial_risk_and_lands_in_journal(monkeypatch):
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot())
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=QTY, price=str(PRICE))], open_after=[])

    # ExitManager 에 최초 1회 확정
    assert bot.exit_manager.initial_risks == [
        (SYM, PRICE * QTY * STOP / 100, STOP)]      # 139 × 10,000 × 5% = 69,500

    er = _entry_risk_of(bot.trade_journal)
    assert er is not None
    assert Decimal(er["initial_risk_amount"]) == Decimal("69500")
    assert Decimal(er["actual_stop_pct"]) == STOP
    assert er["filled_quantity"] == QTY
    assert Decimal(er["entry_cost"]) == PRICE * QTY
    # 계획값(매수수수료 포함)은 덮어쓰지 않고 델타로 남는다
    assert Decimal(er["planned_risk_amount"]) == Decimal("69509.75")
    assert Decimal(er["planned_vs_filled_risk_delta"]) == Decimal("-9.75")
    # 캐시 누수 없음 (완결 → 정리)
    assert sched._entry_fill_lots == {}


# ── 2. 2회 부분체결 — 완결 시 1회만 확정, 저널 갱신 ─────────────────────────────

def test_partial_fills_confirm_once_on_completion_and_update_journal(monkeypatch):
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot())
    order_id = f"o-{SYM}"

    # 1주기: 100주 부분체결 — 주문은 아직 열려 있음
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=100, price=str(PRICE))], open_after=[order_id])
    assert bot.exit_manager.initial_risks == []          # 첫 체결에 확정 없음
    er1 = _entry_risk_of(bot.trade_journal)
    assert er1 is not None and "initial_risk_amount" not in er1   # 계획값만
    assert sched._entry_fill_lots                                  # 누적 보관 중

    # 저널이 이미 레코드를 만들었다 (trade_id 부여)
    bot.engine.portfolio.positions[SYM].trade_id = "T_1"

    # 2주기: 잔여 39주 체결 → 주문 완결
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=39, price=str(PRICE))], open_after=[])

    assert bot.exit_manager.initial_risks == [(SYM, Decimal("69500"), STOP)]
    assert len(bot.trade_journal.entries) == 1           # record_entry 는 1회뿐
    assert len(bot.trade_journal.updates) == 1           # 완결 시 1회 갱신
    tid, patch = bot.trade_journal.updates[0]
    assert tid == "T_1"
    er2 = patch["entry_risk"]
    assert er2["filled_quantity"] == QTY                 # 100 + 39 누적
    assert Decimal(er2["initial_risk_amount"]) == Decimal("69500")
    assert sched._entry_fill_lots == {}

    # 3주기: 추가 체결이 없어도 중복 확정·중복 갱신 없음
    _drive(monkeypatch, sched, bot, sleeps, [], open_after=[])
    assert len(bot.trade_journal.updates) == 1


# ── 3. 등록 실패 → 재시도 성공 시 확정 ──────────────────────────────────────────

def test_registration_retry_confirms_initial_risk(monkeypatch):
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot())
    _fail_n_times(bot.exit_manager, 1)      # 최초 BUY 등록만 실패

    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=QTY, price=str(PRICE))], open_after=[])
    assert sched._pending_exit_registrations == {SYM}
    assert bot.exit_manager.initial_risks == []
    assert sched._entry_fill_lots            # 재시도 대기 중이라 누적 보관

    bot.engine.portfolio.positions[SYM].trade_id = "T_1"
    _drive(monkeypatch, sched, bot, sleeps, [], open_after=[])   # 재시도 성공

    assert sched._pending_exit_registrations == set()
    assert bot.exit_manager.initial_risks == [(SYM, Decimal("69500"), STOP)]
    assert bot.trade_journal.updates[0][0] == "T_1"
    assert sched._entry_fill_lots == {}


# ── 4. 스냅샷 없는 체결(legacy) → entry_risk 키 자체가 없다 ─────────────────────

def test_fill_without_snapshot_leaves_no_entry_risk_key(monkeypatch):
    sched, bot, sleeps = _setup(monkeypatch, snapshot=None)
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=QTY, price=str(PRICE))], open_after=[])

    ctx = bot.trade_journal.entries[-1]["market_context"] or {}
    assert "entry_risk" not in ctx
    # 등록 자체는 정상, ExitManager 확정은 스냅샷과 무관하게 수행된다(원장 분모는 legacy 처리)
    assert bot.exit_manager.registered


# ── 5. 손절 설정 무효(ValueError) → 확정 생략, 매매·등록 영향 없음 ──────────────

def test_invalid_stop_skips_confirmation_without_affecting_trading(monkeypatch):
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot(), raise_on_resolve=True)
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=QTY, price=str(PRICE))], open_after=[])

    assert bot.exit_manager.initial_risks == []      # 확정 생략
    assert bot.exit_manager.registered               # 등록은 정상
    assert sched._pending_exit_registrations == set()
    er = _entry_risk_of(bot.trade_journal)
    assert er is not None and "initial_risk_amount" not in er    # 계획값만 남는다


# ── 6. 완결 판정 불가(주문 id 식별 불가) → 확정 보류 ────────────────────────────

def test_unknown_completion_does_not_confirm(monkeypatch):
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot())
    bot.broker.fills_seq = [[_buy_fill(SYM, qty=QTY, price=str(PRICE))]]

    async def _get():
        return ["식별불가"] if bot.broker.fills_seq else ["식별불가"]

    bot.broker.get_open_orders = _get
    _fill_check_once(monkeypatch, sched, sleeps)

    assert bot.exit_manager.initial_risks == []
    er = _entry_risk_of(bot.trade_journal)
    assert er is not None and "initial_risk_amount" not in er
    assert sched._entry_fill_lots           # 판정 불가라 정리도 하지 않는다


# ── 7. 캐시 수명: 취소·만료된 주문의 누적은 정리된다 ────────────────────────────

def test_cancelled_order_lot_is_pruned(monkeypatch):
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot())
    order_id = f"o-{SYM}"
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=10, price=str(PRICE))], open_after=[order_id])
    assert sched._entry_fill_lots and bot.exit_manager.initial_risks == []

    # 잔여 주문이 만료·취소돼 미체결 목록에서 사라지고 체결도 더 오지 않음
    _drive(monkeypatch, sched, bot, sleeps, [], open_after=[])
    assert sched._entry_fill_lots == {}


def test_engine_signal_cache_is_not_popped_by_scheduler(monkeypatch):
    """엔진 캐시 수명은 엔진(on_fill 완결·clear_pending)이 관리한다 — 스케줄러는 읽기만."""
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot())
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=100, price=str(PRICE))], open_after=[f"o-{SYM}"])
    assert SYM in bot.engine.risk_manager._pending_signal_cache


# ── 8. 계획 SL ≠ 실제 SL → stop_pct 유지 + actual_stop_pct 에 실제값 ────────────

def test_stop_pct_mismatch_is_recorded_not_silently_overwritten(monkeypatch):
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot(stop="5.0"),
                                stop=Decimal("3.5"))   # 실제 등록 SL 이 다름
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=QTY, price=str(PRICE))], open_after=[])

    er = _entry_risk_of(bot.trade_journal)
    assert er["stop_pct"] == "5.0"                       # 계획값 보존
    assert Decimal(er["actual_stop_pct"]) == Decimal("3.5")
    assert Decimal(er["initial_risk_amount"]) == PRICE * QTY * Decimal("3.5") / 100

    issues = _canary_issues(bot, er)
    assert any(i["issue"] == "stop_pct_mismatch" for i in issues), issues


# ── 9. 저널 레코드 → exporter → canary 왕복 (technical passed) ──────────────────

def _canary_issues(bot, entry_risk):
    """마지막 저널 레코드를 원장으로 바꿔 canary 기술 검증만 돌린다."""
    e = bot.trade_journal.entries[-1]
    trade = SimpleNamespace(
        id="T_1", symbol=e["symbol"], name=e["name"], entry_time=datetime(2026, 9, 14, 10, 30),
        entry_price=e["entry_price"], entry_quantity=e["entry_quantity"],
        entry_strategy=e["entry_strategy"], market_context={"entry_risk": entry_risk},
        exit_time=None, exit_price=None, exit_quantity=0, exit_reason="", exit_type="", pnl=None,
    )
    ledger = exporter.build_ledger([trade], {}, applied_sha=SHA)
    return canary.technical_issues(ledger["positions"], ledger["positions"])


def test_journal_record_roundtrips_to_canary_technical_passed(tmp_path, monkeypatch):
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot())
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=QTY, price=str(PRICE))], open_after=[])

    er = _entry_risk_of(bot.trade_journal)
    e = bot.trade_journal.entries[-1]
    trade = SimpleNamespace(
        id="T_1", symbol=e["symbol"], name=e["name"], entry_time=datetime(2026, 9, 14, 10, 30),
        entry_price=e["entry_price"], entry_quantity=e["entry_quantity"],
        entry_strategy=e["entry_strategy"], market_context={"entry_risk": er},
        exit_time=None, exit_price=None, exit_quantity=0, exit_reason="", exit_type="", pnl=None,
    )
    ledger = exporter.build_ledger([trade], {}, applied_sha=SHA)
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, default=str), encoding="utf-8")
    out = tmp_path / "report.json"

    assert canary.main(["--input", str(ledger_path), "--cohort", "risk-sepa_trend-v1",
                        "--output", str(out), "--sha", SHA]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["technical_status"] == "passed", report["technical_issues"]
    assert report["sample"]["excluded"]["open"] == 1        # 미청산 → 성과 표본 제외


# ── 10. 추가 매수 주문은 위험을 분리 기록한다 ───────────────────────────────────

def test_second_order_gets_its_own_lot(monkeypatch):
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot())
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=QTY, price=str(PRICE))], open_after=[])
    first = list(bot.exit_manager.initial_risks)
    er_first = _entry_risk_of(bot.trade_journal)
    assert Decimal(er_first["initial_risk_amount"]) == Decimal("69500")

    bot.engine.portfolio.positions[SYM].trade_id = "T_1"
    second = Fill(order_id="o2", symbol=SYM, side=OrderSide.BUY, quantity=10, price=PRICE)
    _drive(monkeypatch, sched, bot, sleeps, [second], open_after=[])

    # 두 번째 주문도 자기 체결만으로 확정하지만 ExitManager 분모는 최초 1회로 불변
    assert bot.exit_manager.initial_risks == first
    assert sched._entry_fill_lots == {}
    # 원장 계층도 불변 — 기존 거래(T_1)의 entry_risk 를 두 번째 lot 이 덮어쓰지 않는다
    assert bot.trade_journal.updates == []
    assert bot.trade_journal.get_trade("T_1").market_context["entry_risk"] == er_first


# ── 11. 다중 부분체결 → exporter → canary 왕복 (technical passed) ───────────────

def test_multi_partial_fill_roundtrips_to_canary_technical_passed(tmp_path, monkeypatch):
    """저널 entry_quantity 는 첫 체결(100주)로 고정 — exporter 가 스냅샷 누적으로 보정해야 한다."""
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot())
    order_id = f"o-{SYM}"
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=100, price=str(PRICE))], open_after=[order_id])
    bot.engine.portfolio.positions[SYM].trade_id = "T_1"
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=39, price=str(PRICE))], open_after=[])

    e = bot.trade_journal.entries[-1]
    assert e["entry_quantity"] == 100                       # 저널은 첫 체결로 고정
    er = bot.trade_journal.get_trade("T_1").market_context["entry_risk"]
    assert er["filled_quantity"] == QTY                     # 확정값은 누적 139주

    trade = SimpleNamespace(
        id="T_1", symbol=e["symbol"], name=e["name"], entry_time=datetime(2026, 9, 14, 10, 30),
        entry_price=e["entry_price"], entry_quantity=e["entry_quantity"],
        entry_strategy=e["entry_strategy"], market_context={"entry_risk": er},
        exit_time=None, exit_price=None, exit_quantity=0, exit_reason="", exit_type="", pnl=None,
    )
    ledger = exporter.build_ledger([trade], {}, applied_sha=SHA)
    buys = [f for f in ledger["positions"][0]["fills"] if f["side"] == "buy"]
    assert [b["quantity"] for b in buys] == [QTY]
    assert Decimal(buys[0]["price"]) * QTY == PRICE * QTY

    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, default=str), encoding="utf-8")
    out = tmp_path / "report.json"
    assert canary.main(["--input", str(ledger_path), "--cohort", "risk-sepa_trend-v1",
                        "--output", str(out), "--sha", SHA]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["technical_status"] == "passed", report["technical_issues"]


# ── 12. 계측 예외는 체결 처리·저널을 끊지 않는다 ───────────────────────────────

def test_confirmation_exception_does_not_break_fill_processing(monkeypatch):
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot())

    def _boom(*a, **kw):
        raise RuntimeError("계측 경로 예외")

    monkeypatch.setattr(sched, "_confirm_entry_risk", _boom)
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=QTY, price=str(PRICE))], open_after=[])

    assert bot.exit_manager.registered                  # 등록 완료
    assert len(bot.trade_journal.entries) == 1          # 저널 BUY 기록까지 진행
    assert bot.exit_manager.initial_risks == []         # 계측만 생략
    assert sched._pending_exit_registrations == set()


# ── 13. 부분체결 후 잔여 취소 → 누적 체결로 확정 (주문 종료 = 완료) ────────────

def test_cancelled_remainder_confirms_with_accumulated_fills(monkeypatch):
    sched, bot, sleeps = _setup(monkeypatch, snapshot=_snapshot())
    _drive(monkeypatch, sched, bot, sleeps,
           [_buy_fill(SYM, qty=100, price=str(PRICE))], open_after=[f"o-{SYM}"])
    assert bot.exit_manager.initial_risks == []
    bot.engine.portfolio.positions[SYM].trade_id = "T_1"

    # 잔여 39주가 취소·만료돼 미체결 목록에서 사라짐 (추가 체결 없음)
    _drive(monkeypatch, sched, bot, sleeps, [], open_after=[])

    assert bot.exit_manager.initial_risks == [(SYM, Decimal("50000"), STOP)]   # 체결 100주 기준
    assert bot.trade_journal.updates and bot.trade_journal.updates[0][0] == "T_1"
    assert Decimal(bot.trade_journal.updates[0][1]["entry_risk"]["initial_risk_amount"]) == Decimal("50000")
    assert sched._entry_fill_lots == {}
